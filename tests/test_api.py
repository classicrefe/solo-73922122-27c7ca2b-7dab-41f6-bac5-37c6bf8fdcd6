"""API 层测试：HTTP 状态、校验、幂等、查询、并发与 /docs。"""

import threading

from tests.conftest import empty_mask, mask


def _body(batch: str, tr: int, tc: int, m) -> dict:
    return {"batch_id": batch, "tile_row": tr, "tile_col": tc, "mask": m}


def test_health_and_docs(client):
    assert client.get("/health").json() == {"status": "ok", "version": 0}
    docs = client.get("/docs")
    assert docs.status_code == 200
    openapi = client.get("/openapi.json").json()
    assert "/corrections" in openapi["paths"]


def test_post_correction_and_query_patches(client):
    r = client.post("/corrections", json=_body("b1", 0, 0, mask({0: "###............."})))
    assert r.status_code == 201
    receipt = r.json()
    assert receipt["version"] == 1
    assert receipt["parent_version"] == 0
    assert receipt["superseded_patch_ids"] == []
    assert receipt["changed_patches"][0]["patch_id"] == "(0,0)"
    assert receipt["changed_patches"][0]["pixel_count"] == 3
    assert receipt["retransmitted"] is False

    pl = client.get("/patches").json()
    assert pl["version"] == 1 and pl["patch_count"] == 1
    assert pl["patches"][0]["pixels"][:3] == [[0, 0], [0, 1], [0, 2]]

    tiles = client.get("/tiles").json()
    assert tiles["tile_count"] == 1
    assert tiles["tiles"][0]["water_pixel_count"] == 3


def test_retransmission_200_and_conflict_409(client):
    body = _body("dup", 0, 0, mask({0: "#..............."}))
    r1 = client.post("/corrections", json=body)
    assert r1.status_code == 201
    r2 = client.post("/corrections", json=body)
    assert r2.status_code == 200  # 同内容重传
    assert r2.json()["retransmitted"] is True
    assert r2.json()["version"] == r1.json()["version"]

    conflict = client.post(
        "/corrections", json=_body("dup", 0, 0, mask({0: "##.............."}))
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "idempotency_conflict"

    # 失败后仍供出最近成功版
    assert client.get("/health").json()["version"] == 1
    assert client.get("/patches").json()["patch_count"] == 1


def test_invalid_mask_shape_rejected(client):
    bad = _body("bad", 0, 0, [[0] * 15] * 16)
    r = client.post("/corrections", json=bad)
    assert r.status_code == 422

    bad2 = _body("bad2", 0, 0, [[2] * 16] * 16)
    assert client.post("/corrections", json=bad2).status_code == 422

    bad3 = {"batch_id": "   ", "tile_row": 0, "tile_col": 0, "mask": empty_mask()}
    assert client.post("/corrections", json=bad3).status_code == 422


def test_get_receipt_by_batch_and_404(client):
    client.post("/corrections", json=_body("b1", 0, 0, mask({1: "#..............."})))
    got = client.get("/corrections/b1")
    assert got.status_code == 200
    assert got.json()["batch_id"] == "b1"
    assert client.get("/corrections/nope").status_code == 404


def test_patches_filter_by_tile_keeps_cross_tile_patch(client):
    line = mask({8: "################"})
    client.post("/corrections", json=_body("b1", 0, 0, line))
    client.post("/corrections", json=_body("b2", 0, 1, line))
    # 合并为一个跨片斑块
    allp = client.get("/patches").json()
    assert allp["patch_count"] == 1 and allp["patches"][0]["pixel_count"] == 32

    filtered = client.get("/patches", params={"tile_row": 0, "tile_col": 1}).json()
    assert filtered["patch_count"] == 1
    # 过滤结果仍给出斑块的完整像元，而不只是片内部分
    assert filtered["patches"][0]["pixel_count"] == 32


def test_concurrent_http_corrections(client):
    errors: list[int] = []
    lock = threading.Lock()

    def worker(i: int):
        r = client.post(
            "/corrections",
            json=_body(f"c{i}", i, 3, mask({0: "#..............."})),
        )
        if r.status_code != 201:
            with lock:
                errors.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    pl = client.get("/patches").json()
    assert pl["version"] == 10 and pl["patch_count"] == 10

    # 版本链完整：每个批号的回执 parent_version 互不重复且覆盖 0..9
    versions = sorted(
        client.get(f"/corrections/c{i}").json()["parent_version"] for i in range(10)
    )
    assert versions == list(range(10))
