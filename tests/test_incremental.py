"""增量斑块重算的业务测试：跨片连通、拆分、合并、增量范围、幂等、回滚。"""

import threading

import pytest

from app.db import (
    IdempotencyConflict,
    apply_correction,
    current_version,
    get_receipt,
    list_patches,
)
from app.schemas import CorrectionRequest
from tests.conftest import empty_mask, mask


def req(batch: str, tr: int, tc: int, m) -> CorrectionRequest:
    return CorrectionRequest(batch_id=batch, tile_row=tr, tile_col=tc, mask=m)


def patch_map(conn):
    _, patches = list_patches(conn)
    return {p.patch_id: p for p in patches}


# ---------------------------------------------------------------------------
# 1. 跨片四邻接连通 + 斑块编号取全局最小像元
# ---------------------------------------------------------------------------
def test_cross_tile_connectivity_and_global_id(conn):
    # 瓦片(0,0)：底行 (15,0) 起一条水平线
    r1 = apply_correction(conn, req("b1", 0, 0, mask({15: "##.............."})))
    assert r1.version == 1 and r1.parent_version == 0
    assert [p.patch_id for p in r1.changed_patches] == ["(15,0)"]

    # 瓦片(1,0)：顶行 (16,0)、(16,1) 水面，与上面 (15,0)(15,1) 四邻接
    r2 = apply_correction(conn, req("b2", 1, 0, mask({0: "##.............."})))
    assert r2.version == 2
    # 两片应合并成一个跨片斑块，编号取全局最小 (15,0)
    pm = patch_map(conn)
    assert set(pm) == {"(15,0)"}
    p = pm["(15,0)"]
    assert p.tiles == [[0, 0], [1, 0]]
    assert p.pixel_count == 4
    # 回执：原 (15,0) 斑块因并入新像元而改形作废（(16,0) 是本次新瓦片上的
    # 全新像元，重算前并不存在斑块编号，故不在作废清单）。
    assert set(r2.superseded_patch_ids) == {"(15,0)"}
    changed = r2.changed_patches
    assert [x.patch_id for x in changed] == ["(15,0)"]
    assert changed[0].tiles == [[0, 0], [1, 0]]
    assert changed[0].pixel_count == 4


# ---------------------------------------------------------------------------
# 2. 只重算受影响斑块：远处斑块保持不变且不出现在回执里
# ---------------------------------------------------------------------------
def test_only_affected_patches_recomputed(conn):
    apply_correction(conn, req("b1", 0, 0, mask({0: "#..............."})))   # (0,0)
    apply_correction(conn, req("b2", 5, 5, mask({0: "#..............."})))   # 瓦片(5,5) -> (80,80)
    r3 = apply_correction(conn, req("b3", 0, 0, mask({0: "##.............."})))
    # (80,80) 斑块不受影响：不作废、不在 changed 中、依然存在
    assert "(80,80)" not in r3.superseded_patch_ids
    assert all(x.patch_id != "(80,80)" for x in r3.changed_patches)
    pm = patch_map(conn)
    assert "(80,80)" in pm and pm["(80,80)"].pixel_count == 1


# ---------------------------------------------------------------------------
# 3. 原斑块断开后，落在其他瓦片上的部分也要更新（拆分场景）
# ---------------------------------------------------------------------------
def test_split_updates_parts_on_other_tiles(conn):
    # 两个横向相邻瓦片各画一条贯穿的水平水面带（行 8），合并为一条长斑块
    line = mask({8: "################"})
    apply_correction(conn, req("b1", 0, 0, line))
    apply_correction(conn, req("b2", 0, 1, line))
    pm = patch_map(conn)
    assert set(pm) == {"(8,0)"}
    assert pm["(8,0)"].pixel_count == 32
    assert pm["(8,0)"].tiles == [[0, 0], [0, 1]]

    # 把瓦片(0,0)的行 8 全部抹掉 -> 长斑块断开，瓦片(0,1)的部分成为独立斑块
    r3 = apply_correction(conn, req("b3", 0, 0, empty_mask()))
    pm = patch_map(conn)
    assert set(pm) == {"(8,16)"}  # 残部最小全局像元
    assert pm["(8,16)"].tiles == [[0, 1]]
    assert pm["(8,16)"].pixel_count == 16
    assert r3.superseded_patch_ids == ["(8,0)"]
    assert [x.patch_id for x in r3.changed_patches] == ["(8,16)"]


# ---------------------------------------------------------------------------
# 4. 合并：两片水面通过更正片桥接
# ---------------------------------------------------------------------------
def test_merge_three_patches_via_bridge(conn):
    # 左右瓦片各放一个孤立像元，中间瓦片的更正把桥接上
    apply_correction(conn, req("L", 0, 0, mask({8: "...............#"})))  # (8,15)
    apply_correction(conn, req("R", 0, 2, mask({8: "#..............."})))  # (8,32)
    # 中间瓦片行 8 全水面 (8,16)..(8,31)
    r = apply_correction(conn, req("M", 0, 1, mask({8: "################"})))
    pm = patch_map(conn)
    assert set(pm) == {"(8,15)"}
    assert pm["(8,15)"].pixel_count == 18
    assert pm["(8,15)"].tiles == [[0, 0], [0, 1], [0, 2]]
    assert set(r.superseded_patch_ids) == {"(8,15)", "(8,32)"}


# ---------------------------------------------------------------------------
# 5. 边界邻接但未真正连接的斑块不受影响（只对角接触）
# ---------------------------------------------------------------------------
def test_diagonal_neighbor_not_affected(conn):
    apply_correction(conn, req("b1", 0, 0, mask({15: "#..............."})))  # (15,0)
    # 瓦片(1,0) 顶行只在列 1 有水 (16,1)，与 (15,0) 仅对角
    r2 = apply_correction(conn, req("b2", 1, 0, mask({0: ".#.............."})))
    pm = patch_map(conn)
    assert set(pm) == {"(15,0)", "(16,1)"}
    # (15,0) 未被拆分/合并，不作废；(16,1) 是全新斑块，也无作废编号
    assert r2.superseded_patch_ids == []
    assert [x.patch_id for x in r2.changed_patches] == ["(16,1)"]


# ---------------------------------------------------------------------------
# 6. 幂等：同批号同内容重传返回原回执；异内容拒收
# ---------------------------------------------------------------------------
def test_idempotency_same_content_returns_original_receipt(conn):
    r1 = apply_correction(conn, req("dup", 0, 0, mask({0: "#..............."})))
    r2 = apply_correction(conn, req("dup", 0, 0, mask({0: "#..............."})))
    assert r2.retransmitted is True
    assert r2.version == r1.version
    assert r2.parent_version == r1.parent_version
    assert current_version(conn) == r1.version  # 版本不增长
    assert get_receipt(conn, "dup").version == r1.version


def test_idempotency_different_content_rejected(conn):
    apply_correction(conn, req("dup", 0, 0, mask({0: "#..............."})))
    with pytest.raises(IdempotencyConflict):
        apply_correction(conn, req("dup", 0, 0, mask({0: "##.............."})))
    # 同批号换瓦片也算异内容
    with pytest.raises(IdempotencyConflict):
        apply_correction(conn, req("dup", 1, 0, mask({0: "#..............."})))
    # 数据未被污染
    pm = patch_map(conn)
    assert set(pm) == {"(0,0)"}
    assert current_version(conn) == 1


# ---------------------------------------------------------------------------
# 7. 全空掩膜：清空瓦片、斑块正常消失，版本仍推进
# ---------------------------------------------------------------------------
def test_empty_mask_removes_tile_water(conn):
    apply_correction(conn, req("b1", 0, 0, mask({0: "##.............."})))
    r2 = apply_correction(conn, req("b2", 0, 0, empty_mask()))
    assert r2.version == 2
    assert r2.superseded_patch_ids == ["(0,0)"]
    assert r2.changed_patches == []
    _, patches = list_patches(conn)
    assert patches == []


# ---------------------------------------------------------------------------
# 8. 负瓦片坐标
# ---------------------------------------------------------------------------
def test_negative_tile_coordinates(conn):
    r = apply_correction(conn, req("neg", -1, -1, mask({15: "#..............."})))
    # 瓦片(-1,-1) 原点 (-16,-16)，行 15 -> 全局行 -1，列 -16
    assert [p.patch_id for p in r.changed_patches] == ["(-1,-16)"]
    pm = patch_map(conn)
    assert pm["(-1,-16)"].tiles == [[-1, -1]]


# ---------------------------------------------------------------------------
# 9. 并发更正：多连接多线程串行入仓，版本连续，数据完整
# ---------------------------------------------------------------------------
def test_concurrent_data_layer(tmp_path):
    from app.db import connect, init_db

    db = tmp_path / "conc.db"
    c = connect(db)
    init_db(c)

    errors: list[Exception] = []

    def worker(i: int):
        try:
            # 每个线程独立连接，模拟多客户端；写同一库
            cc = connect(db)
            try:
                apply_correction(
                    cc,
                    req(f"batch-{i}", i, 0, mask({0: "#..............."}))
                )
            finally:
                cc.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert current_version(c) == 8
    _, patches = list_patches(c)
    assert len(patches) == 8
    c.close()


# ---------------------------------------------------------------------------
# 10. 失败回滚：模拟写库中途异常，版本与数据保持最近成功版
# ---------------------------------------------------------------------------
def test_failure_keeps_last_successful_version(conn):
    r1 = apply_correction(conn, req("ok", 0, 0, mask({0: "#..............."})))
    assert current_version(conn) == 1

    real_executemany = conn.executemany

    class FailingConn:
        """委托真实连接，仅在写斑块像元时注入故障。"""

        def executemany(self, sql, params):
            if sql.startswith("INSERT INTO patch_pixels"):
                raise RuntimeError("simulated disk failure")
            return real_executemany(sql, params)

        def __getattr__(self, name):
            return getattr(conn, name)

    with pytest.raises(RuntimeError):
        apply_correction(FailingConn(), req("fail", 0, 1, mask({0: "#..............."})))

    # 最近成功版仍可读、数据完整
    assert current_version(conn) == 1
    pm = patch_map(conn)
    assert set(pm) == {"(0,0)"}
    assert get_receipt(conn, "fail") is None
    assert get_receipt(conn, "ok").version == r1.version
