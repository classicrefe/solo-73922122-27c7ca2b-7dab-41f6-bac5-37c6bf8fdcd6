"""WKT 面转换测试：坐标映射、覆盖一致性、内孔、掐点 MULTIPOLYGON、确定性。"""

import re

from app.geometry import pixels_to_wkt


# ---------------------------------------------------------------------------
# 极简 WKT 解析 / 几何工具（仅支持本模块输出的 POLYGON/MULTIPOLYGON）
# ---------------------------------------------------------------------------
def parse_wkt(wkt: str):
    """返回 [(外环, [孔环...]), ...]，环为 [(x, y), ...]（不含闭合重复点）。"""
    assert wkt.startswith("POLYGON(") or wkt.startswith("MULTIPOLYGON((")
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", wkt)]
    pts = list(zip(nums[0::2], nums[1::2]))
    # 按括号结构切环
    rings_txt = re.findall(r"\(([^()]+)\)", wkt)
    rings = []
    for rt in rings_txt:
        coords = [tuple(map(float, p.split())) for p in rt.split(",")]
        assert coords[0] == coords[-1], "环必须闭合"
        rings.append(coords[:-1])
    assert sum(len(r) + 1 for r in rings) == len(pts)
    if wkt.startswith("POLYGON("):
        return [(rings[0], rings[1:])]
    # MULTIPOLYGON：按 ")),((" 切分各部分
    body = wkt[len("MULTIPOLYGON(") : -1]
    parts = re.split(r"\)\s*,\s*\(", body[1:-1]) if ")),((" not in body else None
    # 更稳妥：重新按部分解析
    parts = []
    depth = 0
    start = None
    for i, ch in enumerate(body):
        if ch == "(":
            if depth == 1:
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 1 and start is not None:
                parts.append(body[start : i + 1])
                start = None
    out = []
    for p in parts:
        rs = [
            [tuple(map(float, q.split())) for q in rt.split(",")]
            for rt in re.findall(r"\(([^()]+)\)", p)
        ]
        rs = [r[:-1] for r in rs]
        out.append((rs[0], rs[1:]))
    return out


def signed_area(ring) -> float:
    s = 0.0
    for i, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(i + 1) % len(ring)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def point_in_ring(px, py, ring) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            if px < x1 + (py - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def covered_by(wkt: str, r: int, c: int) -> bool:
    """像元 (r,c) 的中心点是否被该 WKT 面覆盖。"""
    px, py = c + 0.5, -r - 0.5
    for ext, holes in parse_wkt(wkt):
        if point_in_ring(px, py, ext) and not any(
            point_in_ring(px, py, h) for h in holes
        ):
            return True
    return False


def total_area(wkt: str) -> float:
    return sum(
        abs(signed_area(ext)) - sum(abs(signed_area(h)) for h in holes)
        for ext, holes in parse_wkt(wkt)
    )


def assert_wkt_matches_pixels(wkt: str, pixels: set[tuple[int, int]]):
    """覆盖范围须与水像元并集一致：面积相等且每个像元中心被覆盖。"""
    assert total_area(wkt) == float(len(pixels))
    for r, c in pixels:
        assert covered_by(wkt, r, c), f"像元 {(r, c)} 未被覆盖"
    for ext, holes in parse_wkt(wkt):
        assert signed_area(ext) > 0, "外环须为逆时针 CCW"
        for h in holes:
            assert signed_area(h) < 0, "内孔环须为顺时针 CW"


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def test_single_pixel_exact_wkt_and_axis_mapping():
    # 像元 (0,0)：左上角 (0,0)，覆盖 x∈[0,1]，y∈[-1,0]
    wkt = pixels_to_wkt({(0, 0)})
    assert wkt == "POLYGON((0 -1, 1 -1, 1 0, 0 0, 0 -1))"
    assert_wkt_matches_pixels(wkt, {(0, 0)})


def test_negative_coordinates_mapping():
    # 像元 (-1,-16)：左上角 (-16, 1)
    wkt = pixels_to_wkt({(-1, -16)})
    assert wkt == "POLYGON((-16 0, -15 0, -15 1, -16 1, -16 0))"
    assert_wkt_matches_pixels(wkt, {(-1, -16)})


def test_block_2x2_single_ring():
    px = {(r, c) for r in range(2) for c in range(2)}
    wkt = pixels_to_wkt(px)
    parts = parse_wkt(wkt)
    assert len(parts) == 1 and parts[0][1] == []
    assert total_area(wkt) == 4.0
    assert_wkt_matches_pixels(wkt, px)


def test_hole_preserved():
    # 3x3 去掉中心：外环面积 9，孔面积 1，净 8
    px = {(r, c) for r in range(3) for c in range(3)} - {(1, 1)}
    wkt = pixels_to_wkt(px)
    parts = parse_wkt(wkt)
    assert len(parts) == 1
    ext, holes = parts[0]
    assert len(holes) == 1
    assert abs(signed_area(ext)) == 9.0
    assert abs(signed_area(holes[0])) == 1.0
    assert_wkt_matches_pixels(wkt, px)
    assert not covered_by(wkt, 1, 1)  # 孔内像元不覆盖


def test_u_shape_no_hole():
    # U 形：开口不算孔
    px = {(0, 0), (0, 2), (1, 0), (1, 1), (1, 2)}
    wkt = pixels_to_wkt(px)
    parts = parse_wkt(wkt)
    assert len(parts) == 1 and parts[0][1] == []
    assert_wkt_matches_pixels(wkt, px)


def test_diagonal_pair_becomes_multipolygon():
    # 仅对角相接 -> 两个部分（单个自交外环会是无效面）
    px = {(0, 0), (1, 1)}
    wkt = pixels_to_wkt(px)
    assert wkt.startswith("MULTIPOLYGON(")
    parts = parse_wkt(wkt)
    assert len(parts) == 2
    assert_wkt_matches_pixels(wkt, px)


def test_connected_pinch_hole_touching_exterior():
    # 四邻接连通，内孔 (1,0) 与外边界在顶点 (1,-1) 处仅点接触（补集掐点）。
    # 孔保留为独立孔环（与外环在该顶点相切，OGC 允许），覆盖精确。
    px = {(0, 0), (0, -1), (1, -1), (2, -1), (2, 0), (2, 1), (1, 1)}
    wkt = pixels_to_wkt(px)
    parts = parse_wkt(wkt)
    assert len(parts) == 1
    ext, holes = parts[0]
    assert len(holes) == 1
    assert abs(signed_area(ext)) == 8.0
    assert abs(signed_area(holes[0])) == 1.0
    assert_wkt_matches_pixels(wkt, px)
    assert not covered_by(wkt, 1, 0)  # 孔内不覆盖


def test_two_holes_touching_at_point():
    # 5x5 去掉 (1,1) 与 (2,2)：两个内孔仅对角点接触 -> 两个独立孔环
    px = {(r, c) for r in range(5) for c in range(5)} - {(1, 1), (2, 2)}
    wkt = pixels_to_wkt(px)
    parts = parse_wkt(wkt)
    assert len(parts) == 1
    ext, holes = parts[0]
    assert len(holes) == 2
    assert_wkt_matches_pixels(wkt, px)
    assert not covered_by(wkt, 1, 1)
    assert not covered_by(wkt, 2, 2)


def test_hole_touching_shell_at_point_keeps_exact_coverage():
    # 3x3 去掉 (0,0) 与 (1,1)：孔 (1,1) 在顶点 (1,-1) 处与外边界点接触。
    px = {(r, c) for r in range(3) for c in range(3)} - {(0, 0), (1, 1)}
    wkt = pixels_to_wkt(px)
    # 覆盖范围精确，孔保留（与外环单点相切，OGC 允许）
    assert_wkt_matches_pixels(wkt, px)


def test_deterministic_regardless_of_input_order():
    px = {(r, c) for r in range(3) for c in range(3)} - {(1, 1)}
    assert pixels_to_wkt(set(px)) == pixels_to_wkt(set(reversed(sorted(px))))


def test_empty_raises():
    import pytest

    with pytest.raises(ValueError):
        pixels_to_wkt(set())
