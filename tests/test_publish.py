"""发布流程的数据层测试：快照/转换/整版提交、增量沿用、冲突、失败安全。"""

import threading

import pytest

from app import db
from app.db import (
    PublishConflict,
    apply_correction,
    build_manifest,
    commit_manifest,
    connect,
    current_version,
    get_current_manifest,
    init_db,
    publish,
    snapshot_for_publish,
)
from app.geometry import pixels_to_wkt
from app.schemas import CorrectionRequest
from tests.conftest import empty_mask, mask


@pytest.fixture()
def env(tmp_path):
    """每个测试一个全新的库文件与主连接，发布快照走独立只读连接。"""
    path = tmp_path / "pub.db"
    c = connect(path)
    init_db(c)
    yield c, path
    c.close()


def req(batch: str, tr: int, tc: int, m) -> CorrectionRequest:
    return CorrectionRequest(batch_id=batch, tile_row=tr, tile_col=tc, mask=m)


# ---------------------------------------------------------------------------
# 1. 基本发布与查询
# ---------------------------------------------------------------------------
def test_publish_then_query_manifest(env):
    conn, path = env
    apply_correction(conn, req("b1", 0, 0, mask({0: "##.............."})))
    apply_correction(conn, req("b2", 5, 5, mask({0: "#..............."})))
    assert current_version(conn) == 2

    receipt = publish(conn, path, 2)
    assert receipt.release_no == 1
    assert receipt.source_version == 2
    assert receipt.patch_count == 2
    assert receipt.converted_count == 2
    assert receipt.reused_count == 0
    assert receipt.removed_count == 0

    manifest = get_current_manifest(conn)
    assert manifest is not None
    assert manifest.release_no == 1
    assert manifest.source_version == 2
    assert manifest.patch_count == 2
    by_id = {p.patch_id: p for p in manifest.patches}
    assert set(by_id) == {"(0,0)", "(80,80)"}
    # 边界与像元一致：x=全局列，y=-全局行
    assert by_id["(0,0)"].wkt == pixels_to_wkt({(0, 0), (0, 1)})
    assert by_id["(0,0)"].wkt == "POLYGON((0 -1, 2 -1, 2 0, 0 0, 0 -1))"
    assert by_id["(0,0)"].pixel_count == 2
    assert by_id["(80,80)"].pixel_count == 1


def test_publish_empty_library(env):
    conn, path = env
    receipt = publish(conn, path, 0)
    assert receipt.release_no == 1
    assert receipt.source_version == 0
    assert receipt.patch_count == 0
    manifest = get_current_manifest(conn)
    assert manifest is not None and manifest.patch_count == 0


def test_no_manifest_before_first_publish(env):
    conn, _ = env
    assert get_current_manifest(conn) is None


# ---------------------------------------------------------------------------
# 2. 增量沿用 / 同号改形重转 / 消失移除
# ---------------------------------------------------------------------------
def test_reuse_unchanged_reconvert_reshaped_remove_disappeared(env):
    conn, path = env
    # 斑块 A (0,0) 两像元；远处斑块 B (80,80)
    apply_correction(conn, req("b1", 0, 0, mask({0: "##.............."})))
    apply_correction(conn, req("b2", 5, 5, mask({0: "#..............."})))
    r1 = publish(conn, path, 2)
    assert r1.converted_count == 2
    wkt_b_v1 = {p.patch_id: p.wkt for p in r1.patches}["(80,80)"]

    # 同号改形：A 编号不变仍为 (0,0)，像元从 2 格变 3 格
    apply_correction(conn, req("b3", 0, 0, mask({0: "###............."})))
    r2 = publish(conn, path, 3)
    assert r2.release_no == 2
    assert r2.source_version == 3
    assert r2.converted_count == 1  # 只有 A 重新转换
    assert r2.reused_count == 1     # B 像元集合未变，沿用原边界
    assert r2.removed_count == 0
    by_id = {p.patch_id: p for p in r2.patches}
    assert by_id["(0,0)"].reused is False
    assert by_id["(80,80)"].reused is True
    assert by_id["(80,80)"].wkt == wkt_b_v1  # 原边界原样沿用
    assert by_id["(0,0)"].wkt == pixels_to_wkt({(0, 0), (0, 1), (0, 2)})

    # A 整片抹除 -> 从新清单移除
    apply_correction(conn, req("b4", 0, 0, empty_mask()))
    r3 = publish(conn, path, 4)
    assert r3.removed_count == 1
    assert r3.patch_count == 1
    assert {p.patch_id for p in r3.patches} == {"(80,80)"}
    manifest = get_current_manifest(conn)
    assert {p.patch_id for p in manifest.patches} == {"(80,80)"}
    assert manifest.source_version == 4


def test_republish_same_version_reuses_all(env):
    conn, path = env
    apply_correction(conn, req("b1", 0, 0, mask({0: "#..............."})))
    publish(conn, path, 1)
    r2 = publish(conn, path, 1)  # 版号未变，可重复发布
    assert r2.release_no == 2
    assert r2.converted_count == 0
    assert r2.reused_count == 1


# ---------------------------------------------------------------------------
# 3. 版号冲突：过期版号、未来版号、转换期间有新更正
# ---------------------------------------------------------------------------
def test_conflict_on_stale_or_future_version(env):
    conn, path = env
    apply_correction(conn, req("b1", 0, 0, mask({0: "#..............."})))
    with pytest.raises(PublishConflict) as e1:
        publish(conn, path, 0)  # 过期
    assert e1.value.current_version == 1
    with pytest.raises(PublishConflict):
        publish(conn, path, 2)  # 未来
    assert get_current_manifest(conn) is None  # 冲突不产生清单


def test_correction_during_conversion_causes_conflict(env):
    """转换期间更正仍可入库；提交时版号已变 -> 冲突，原成果保持可查。"""
    conn, path = env
    apply_correction(conn, req("b1", 0, 0, mask({0: "#..............."})))
    publish(conn, path, 1)  # 已有成果 release 1

    # 三段式手工编排：快照后在转换前入库一条更正
    patches, old = snapshot_for_publish(path, 1)
    apply_correction(conn, req("b2", 1, 0, mask({0: "#..............."})))
    assert current_version(conn) == 2  # 更正正常入库，未被转换阻塞
    entries, *_ = build_manifest(patches, old)
    with pytest.raises(PublishConflict) as exc:
        commit_manifest(conn, 1, entries)
    assert exc.value.source_version == 1
    assert exc.value.current_version == 2

    # 原成果（release 1, source 1）仍可查
    manifest = get_current_manifest(conn)
    assert manifest.release_no == 1
    assert manifest.source_version == 1
    assert {p.patch_id for p in manifest.patches} == {"(0,0)"}


def test_full_publish_race_via_monkeypatched_converter(env, monkeypatch):
    """端到端：publish() 转换期间并发更正 -> PublishConflict，更正已入库。"""
    conn, path = env
    apply_correction(conn, req("b1", 0, 0, mask({0: "#..............."})))

    real = db.pixels_to_wkt
    injected = []

    def racing_converter(pixels):
        if not injected:
            # 转换进行中，另一条更正照常入库
            apply_correction(conn, req("b2", 2, 2, mask({0: "#..............."})))
            injected.append(True)
        return real(pixels)

    monkeypatch.setattr(db, "pixels_to_wkt", racing_converter)
    with pytest.raises(PublishConflict):
        publish(conn, path, 1)
    assert injected  # 确实在转换期间插入
    assert current_version(conn) == 2  # 更正已生效
    assert get_current_manifest(conn) is None  # 失败的发布不留清单


# ---------------------------------------------------------------------------
# 4. 失败安全：转换或提交失败，原成果仍可查
# ---------------------------------------------------------------------------
def test_failed_conversion_keeps_previous_manifest(env, monkeypatch):
    conn, path = env
    apply_correction(conn, req("b1", 0, 0, mask({0: "#..............."})))
    publish(conn, path, 1)

    apply_correction(conn, req("b2", 0, 0, mask({0: "##.............."})))

    def boom(pixels):
        raise RuntimeError("simulated GIS engine failure")

    monkeypatch.setattr(db, "pixels_to_wkt", boom)
    with pytest.raises(RuntimeError):
        publish(conn, path, 2)

    manifest = get_current_manifest(conn)  # 原成果仍可查
    assert manifest.release_no == 1
    assert manifest.source_version == 1
    assert {p.patch_id for p in manifest.patches} == {"(0,0)"}


def test_failed_commit_rolls_back(env):
    conn, path = env
    apply_correction(conn, req("b1", 0, 0, mask({0: "#..............."})))
    publish(conn, path, 1)
    apply_correction(conn, req("b2", 1, 0, mask({0: "#..............."})))

    real_executemany = conn.executemany

    class FailingConn:
        """委托真实连接，仅在写发布清单时注入故障。"""

        def executemany(self, sql, params):
            if sql.startswith("INSERT INTO published_patches"):
                raise RuntimeError("simulated disk failure")
            return real_executemany(sql, params)

        def __getattr__(self, name):
            return getattr(conn, name)

    patches, old = snapshot_for_publish(path, 2)
    entries, *_ = build_manifest(patches, old)
    with pytest.raises(RuntimeError):
        commit_manifest(FailingConn(), 2, entries)

    manifest = get_current_manifest(conn)  # 事务回滚，原清单完整
    assert manifest.release_no == 1
    assert manifest.source_version == 1
    assert {p.patch_id for p in manifest.patches} == {"(0,0)"}


# ---------------------------------------------------------------------------
# 5. 并发：发布与更正并发时，更正不丢失、发布要么整版生效要么冲突
# ---------------------------------------------------------------------------
def test_concurrent_publish_and_corrections(tmp_path):
    path = tmp_path / "conc.db"
    c = connect(path)
    init_db(c)
    for i in range(4):
        apply_correction(c, req(f"seed-{i}", 0, i, mask({0: "#..............."})))

    errors: list[Exception] = []
    results: list[str] = []
    lock = threading.Lock()

    def correction_worker(i: int):
        try:
            cc = connect(path)
            try:
                apply_correction(
                    cc, req(f"extra-{i}", 1, i, mask({0: "#..............."}))
                )
            finally:
                cc.close()
            with lock:
                results.append("correction-ok")
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    def publish_worker():
        try:
            for v in range(4, 9):
                try:
                    publish(c, path, v)
                    with lock:
                        results.append(f"published-{v}")
                    break
                except PublishConflict:
                    continue  # 版号被更正推进，按新版号重试
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=correction_worker, args=(i,)) for i in range(4)]
    threads.append(threading.Thread(target=publish_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert results.count("correction-ok") == 4  # 更正全部入库
    assert any(r.startswith("published-") for r in results)  # 发布成功一次
    manifest = get_current_manifest(c)
    assert manifest.source_version == current_version(c)  # 清单对应最终版
    assert manifest.patch_count == 8
    c.close()
