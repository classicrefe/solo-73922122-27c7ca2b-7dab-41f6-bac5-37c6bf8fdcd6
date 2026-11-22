"""数据层单元测试：掩膜编解码、连通分量、斑块编号规则。"""

from app.config import TILE_SIZE
from app.db import (
    build_patch,
    decode_mask,
    encode_mask,
    label_components,
    make_patch_id,
    parse_patch_id,
    pixel_tile,
    tile_origin,
)
from tests.conftest import empty_mask, full_mask, mask


def test_mask_roundtrip_full_and_empty():
    assert decode_mask(encode_mask(full_mask())) == full_mask()
    assert decode_mask(encode_mask(empty_mask())) == empty_mask()


def test_mask_roundtrip_pattern():
    m = mask({0: "1###............", 15: "...............1", 7: "#.##.#.........#"})
    blob = encode_mask(m)
    assert len(blob) == TILE_SIZE * TILE_SIZE // 8
    assert decode_mask(blob) == m


def test_pixel_tile_negative_coords_floor():
    # 负坐标必须按 floor 划分瓦片，不能截断成 0
    assert pixel_tile(0, 0) == (0, 0)
    assert pixel_tile(15, 15) == (0, 0)
    assert pixel_tile(16, 16) == (1, 1)
    assert pixel_tile(-1, -1) == (-1, -1)
    assert pixel_tile(-16, -16) == (-1, -1)
    assert pixel_tile(-17, 0) == (-2, 0)
    assert tile_origin(-1, -1) == (-16, -16)


def test_label_components_four_connectivity_diagonal_not_connected():
    # 对角接触不算连通（四邻接）
    comps = label_components({(0, 0), (1, 1)})
    assert len(comps) == 2

    # 边接触连通
    comps = label_components({(0, 0), (0, 1), (1, 1), (2, 1)})
    assert len(comps) == 1
    assert len(comps[0]) == 4


def test_patch_id_ordering_row_first():
    # 最小像元先比行再比列：(2,0) 小于 (1,99)
    p = build_patch({(2, 0), (1, 99), (5, 5)})
    assert p.patch_id == make_patch_id(1, 99)
    assert parse_patch_id(p.patch_id) == (1, 99)
    assert p.pixels[0] == [1, 99]
    assert p.tiles  # 非空


def test_build_patch_spans_multiple_tiles():
    p = build_patch({(15, 5), (16, 5), (16, 6)})  # 跨 0/1 行瓦片
    assert p.tiles == [[0, 0], [1, 0]]
    assert p.pixel_count == 3
