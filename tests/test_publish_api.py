"""发布 API 的 HTTP 层测试：发布/查询/冲突/校验/失败安全/文档。"""

from tests.conftest import empty_mask, mask


def _body(batch: str, tr: int, tc: int, m) -> dict:
    return {"batch_id": batch, "tile_row": tr, "tile_col": tc, "mask": m}


def _publish(client, version: int):
    return client.post("/publications", json={"source_version": version})


def test_publish_flow_and_query_manifest(client):
    client.post("/corrections", json=_body("b1", 0, 0, mask({0: "##.............."})))
    client.post("/corrections", json=_body("b2", 5, 5, mask({0: "#..............."})))
    assert client.get("/health").json()["version"] == 2

    r = _publish(client, 2)
    assert r.status_code == 201
    receipt = r.json()
    assert receipt["release_no"] == 1
    assert receipt["source_version"] == 2
    assert receipt["patch_count"] == 2
    assert receipt["converted_count"] == 2
    assert receipt["reused_count"] == 0
    assert receipt["removed_count"] == 0
    by_id = {p["patch_id"]: p for p in receipt["patches"]}
    assert by_id["(0,0)"]["wkt"] == "POLYGON((0 -1, 2 -1, 2 0, 0 0, 0 -1))"
    assert by_id["(0,0)"]["reused"] is False

    m = client.get("/publications/current")
    assert m.status_code == 200
    manifest = m.json()
    assert manifest["release_no"] == 1
    assert manifest["source_version"] == 2
    assert manifest["patch_count"] == 2
    ids = {p["patch_id"] for p in manifest["patches"]}
    assert ids == {"(0,0)", "(80,80)"}
    for p in manifest["patches"]:
        assert p["wkt"].startswith("POLYGON((")


def test_manifest_404_before_any_publish(client):
    r = client.get("/publications/current")
    assert r.status_code == 404


def test_publish_conflict_on_stale_version(client):
    client.post("/corrections", json=_body("b1", 0, 0, mask({0: "#..............."})))
    assert _publish(client, 1).status_code == 201

    # 新更正推进版号后，用旧版号发布 -> 409
    client.post("/corrections", json=_body("b2", 0, 0, mask({0: "###............."})))
    r = _publish(client, 1)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "source_version_conflict"
    assert detail["source_version"] == 1
    assert detail["current_version"] == 2

    # 未来版号同样 409
    assert _publish(client, 99).status_code == 409

    # 冲突后原成果仍可查
    m = client.get("/publications/current").json()
    assert m["release_no"] == 1 and m["source_version"] == 1

    # 按最新版号重新发布成功
    r2 = _publish(client, 2)
    assert r2.status_code == 201
    assert r2.json()["release_no"] == 2
    assert client.get("/publications/current").json()["source_version"] == 2


def test_publish_validation_422(client):
    assert client.post("/publications", json={"source_version": -1}).status_code == 422
    assert client.post("/publications", json={}).status_code == 422
    assert client.post("/publications", json={"source_version": "x"}).status_code == 422


def test_reshape_reconverts_and_untouched_reused_over_http(client):
    # 斑块 A：(0,0)(0,1)；远处斑块 B
    client.post("/corrections", json=_body("b1", 0, 0, mask({0: "##.............."})))
    client.post("/corrections", json=_body("b2", 5, 5, mask({0: "#..............."})))
    r1 = _publish(client, 2).json()
    wkt_b = {p["patch_id"]: p["wkt"] for p in r1["patches"]}["(80,80)"]

    # 同号改形：A 编号不变、像元增加一格
    client.post("/corrections", json=_body("b3", 0, 0, mask({0: "###............."})))
    r2 = _publish(client, 3).json()
    assert r2["converted_count"] == 1 and r2["reused_count"] == 1
    by_id = {p["patch_id"]: p for p in r2["patches"]}
    assert by_id["(0,0)"]["reused"] is False
    assert by_id["(0,0)"]["wkt"] == "POLYGON((0 -1, 3 -1, 3 0, 0 0, 0 -1))"
    assert by_id["(80,80)"]["reused"] is True
    assert by_id["(80,80)"]["wkt"] == wkt_b

    # A 整片抹除 -> 从新清单移除
    client.post("/corrections", json=_body("b4", 0, 0, empty_mask()))
    r3 = _publish(client, 4).json()
    assert r3["removed_count"] == 1
    assert {p["patch_id"] for p in r3["patches"]} == {"(80,80)"}
    m = client.get("/publications/current").json()
    assert {p["patch_id"] for p in m["patches"]} == {"(80,80)"}


def test_hole_preserved_in_published_wkt(client):
    # 3x3 水面环（中心为孔）铺在瓦片 (0,0) 左上角
    rows = {0: "###.............", 1: "#.#.............", 2: "###............."}
    client.post("/corrections", json=_body("b1", 0, 0, mask(rows)))
    r = _publish(client, 1)
    assert r.status_code == 201
    wkt = r.json()["patches"][0]["wkt"]
    # 外环 + 一个内孔环
    assert wkt == "POLYGON((0 -3, 3 -3, 3 0, 0 0, 0 -3), (1 -2, 1 -1, 2 -1, 2 -2, 1 -2))"


def test_docs_and_openapi_include_publications(client):
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/publications" in paths
    assert "/publications/current" in paths
