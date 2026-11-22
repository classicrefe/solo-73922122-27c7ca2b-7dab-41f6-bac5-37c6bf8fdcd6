"""像元集合 -> WKT 面（POLYGON / MULTIPOLYGON）。

坐标约定
--------
像元 (row, col) 是单位方格，左上角坐标 = (全局列, 负全局行) = (col, -row)，
即 x 轴取全局列、y 轴取负全局行，方格覆盖 x∈[col, col+1]，y∈[-row-1, -row]。

算法
----
1. 生成有向边界边：沿边界行进方向水面始终在左手侧
   （因此外环为逆时针 CCW、内孔环为顺时针 CW，符合 OGC 右手定则）。
2. 按顶点把边缝合成闭合环。在"掐点"（两个像元仅对角相接的顶点，
   同一顶点有两条出边）处一律左转，使环贴着同一像元的角走。
   左转配对保证对角相接的两片水面拆成各自独立的外环（MULTIPOLYGON），
   而不是一个自交（无效）的外环。
3. 缝合后若某环仍多次经过同一顶点（非水面像元对角掐点所致，
   例如内孔区域与外边界仅点接触），在该顶点处把环拆成多个简单环 ——
   内孔因此保留为独立孔环（可能与外环共享一个顶点）。
4. 按鞋带公式符号面积分类外环（正）/ 内孔（负），
   内孔按其边中点的射线法包含测试归入对应外环。

输出对同一像元集合是确定性的（环旋转到最小顶点起始、外环/内孔/各部分排序），
因此"像元集合相同 => WKT 相同"，与发布侧的边界沿用判定一致。

有效性：环之间仅可能在单个顶点相切（非水面像元对角掐点），OGC 允许
环相切；且四邻接连通的像元集合其内部必然连通，满足 OGC 对面有效性
（内部连通）的要求。已经 GEOS（Shapely is_valid）对随机与掐点用例
交叉验证全部有效。MULTIPOLYGON 分支用于非四连通输入（如仅对角相接的
两片），对四邻接斑块不会触发，仅作防御。
"""

from __future__ import annotations

Vertex = tuple[int, int]  # (x, y)
Ring = list[Vertex]


# ---------------------------------------------------------------------------
# 边界边生成（水在行进方向左手侧）
# ---------------------------------------------------------------------------
def _boundary_edges(pixels: set[tuple[int, int]]) -> dict[Vertex, list[Vertex]]:
    """有向边界边表：起点 -> [终点, ...]。同一顶点至多 2 条出边（掐点）。"""
    out: dict[Vertex, list[Vertex]] = {}

    def add(a: Vertex, b: Vertex) -> None:
        out.setdefault(a, []).append(b)

    for r, c in pixels:
        if (r - 1, c) not in pixels:  # 上边：西行，水在南（左手侧）
            add((c + 1, -r), (c, -r))
        if (r + 1, c) not in pixels:  # 下边：东行，水在北
            add((c, -r - 1), (c + 1, -r - 1))
        if (r, c - 1) not in pixels:  # 左边：南行，水在东
            add((c, -r), (c, -r - 1))
        if (r, c + 1) not in pixels:  # 右边：北行，水在西
            add((c + 1, -r - 1), (c + 1, -r))
    return out


def _cross(d1: Vertex, d2: Vertex) -> int:
    return d1[0] * d2[1] - d1[1] * d2[0]


def _canonical(ring: Ring) -> Ring:
    """旋转环使最小顶点在最前，保证同一几何输出唯一字符串。"""
    i = min(range(len(ring)), key=lambda k: ring[k])
    return ring[i:] + ring[:i]


def _split_self_touching(ring: Ring) -> list[Ring]:
    """把多次经过同一顶点的环在重复顶点处拆成若干简单环。

    简单环不会重复顶点；重复只可能来自非水面像元的对角掐点
    （内孔与外边界、或两个内孔仅点接触）。拆分保持覆盖范围不变。
    """
    out: list[Ring] = []
    stack: Ring = []
    pos: dict[Vertex, int] = {}
    for v in ring:
        if v in pos:
            # 从该顶点开始的子环出栈，成为独立简单环
            i = pos[v]
            out.append(stack[i:])
            stack = stack[:i]
            pos = {x: k for k, x in enumerate(stack)}
        pos[v] = len(stack)
        stack.append(v)
    if stack:
        out.append(stack)
    return out


def _simplify(ring: Ring) -> Ring:
    """去掉直道上的共线中间点，输出更紧凑（几何不变）。"""
    out: Ring = []
    n = len(ring)
    for i, v in enumerate(ring):
        px, py = ring[i - 1]
        nx, ny = ring[(i + 1) % n]
        d1 = (v[0] - px, v[1] - py)
        d2 = (nx - v[0], ny - v[1])
        if _cross(d1, d2) != 0:  # 只保留转向点
            out.append(v)
    return out


def _stitch_rings(edges: dict[Vertex, list[Vertex]]) -> list[Ring]:
    """把有向边缝合成闭合环（顶点不重复，末点不等于起点）。"""
    rings: list[Ring] = []
    while edges:
        start = next(iter(edges))
        ring: Ring = [start]
        prev: Vertex | None = None
        cur = start
        while True:
            cands = edges[cur]
            if len(cands) == 1 or prev is None:
                nxt = cands[0]
            else:
                # 掐点：两条出边必为一左一右，左转 => 贴同一像元角走，环不自交
                d_in = (cur[0] - prev[0], cur[1] - prev[1])
                left = [
                    c
                    for c in cands
                    if _cross(d_in, (c[0] - cur[0], c[1] - cur[1])) > 0
                ]
                nxt = left[0]
            cands.remove(nxt)
            if not cands:
                del edges[cur]
            if nxt == start:
                break
            ring.append(nxt)
            prev, cur = cur, nxt
        for simple in _split_self_touching(ring):
            rings.append(_canonical(_simplify(simple)))
    return rings


def _signed_area(ring: Ring) -> float:
    """鞋带公式符号面积：CCW 为正，CW 为负。"""
    s = 0
    for i, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(i + 1) % len(ring)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _point_in_ring(px: float, py: float, ring: Ring) -> bool:
    """射线法包含测试。查询点取孔边中点（带 .5 坐标），不会落在任何环上。"""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            if px < x1 + (py - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _fmt_ring(ring: Ring) -> str:
    pts = ", ".join(f"{x} {y}" for x, y in ring + [ring[0]])
    return f"({pts})"


def pixels_to_wkt(pixels: set[tuple[int, int]]) -> str:
    """把一个斑块的全部像元转成 WKT 面，覆盖范围 = 像元并集，保留内孔。

    返回 POLYGON（单外环）或 MULTIPOLYGON（对角掐点拆分出的多个部分）。
    """
    if not pixels:
        raise ValueError("空像元集合无法生成面")
    rings = _stitch_rings(_boundary_edges(pixels))
    exteriors = sorted(
        (r for r in rings if _signed_area(r) > 0), key=lambda r: r[0]
    )
    holes = [r for r in rings if _signed_area(r) < 0]

    parts: list[list] = [[ext, []] for ext in exteriors]
    for h in holes:
        (ax, ay), (bx, by) = h[0], h[1]
        mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
        containing = [
            i for i, (ext, _) in enumerate(parts) if _point_in_ring(mx, my, ext)
        ]
        if not containing:
            raise ValueError("内孔无法归入任何外环，像元集合异常")
        # 正常数据恰有一个包含外环；取面积最小者兜底
        idx = min(containing, key=lambda i: abs(_signed_area(parts[i][0])))
        parts[idx][1].append(h)

    for _, hs in parts:
        hs.sort(key=lambda r: r[0])

    if len(parts) == 1:
        ext, hs = parts[0]
        body = ", ".join([_fmt_ring(ext)] + [_fmt_ring(h) for h in hs])
        return f"POLYGON({body})"
    polys = []
    for ext, hs in parts:
        body = ", ".join([_fmt_ring(ext)] + [_fmt_ring(h) for h in hs])
        polys.append(f"({body})")
    return f"MULTIPOLYGON({', '.join(polys)})"
