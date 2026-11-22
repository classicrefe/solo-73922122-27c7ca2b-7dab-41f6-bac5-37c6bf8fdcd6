"""测试公共夹具与掩膜构造工具。"""

from __future__ import annotations

import itertools
import os
import tempfile
from pathlib import Path

import pytest

# 在导入 app 之前指定临时数据库目录
_tmpdir = tempfile.mkdtemp(prefix="water-test-")
os.environ.setdefault("WATER_DB_PATH", str(Path(_tmpdir) / "default.db"))

from app.config import TILE_SIZE  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402

_counter = itertools.count()


def mask(rows: dict[int, str] | None = None) -> list[list[int]]:
    """用字符串快速构造 16x16 掩膜，'#'/ '1' = 水面。未给的行全 0。"""
    m = [[0] * TILE_SIZE for _ in range(TILE_SIZE)]
    for r, line in (rows or {}).items():
        for c, ch in enumerate(line):
            if ch in "#1":
                m[r][c] = 1
    return m


def full_mask() -> list[list[int]]:
    return [[1] * TILE_SIZE for _ in range(TILE_SIZE)]


def empty_mask() -> list[list[int]]:
    return [[0] * TILE_SIZE for _ in range(TILE_SIZE)]


def _fresh_db_path() -> Path:
    return Path(_tmpdir) / f"db-{os.getpid()}-{next(_counter)}.db"


@pytest.fixture()
def conn():
    """每个测试一个全新的临时库与独立连接。"""
    path = _fresh_db_path()
    c = connect(path)
    init_db(c)
    yield c
    c.close()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个 API 测试独立库的 TestClient。"""
    from fastapi.testclient import TestClient

    db_file = tmp_path / "api.db"
    from app import config, main

    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(main, "DB_PATH", db_file)
    # 重置 lifespan 中建立的连接，强制按新路径重连
    if hasattr(app.state, "conn"):
        app.state.conn.close()
    with TestClient(app) as cl:
        yield cl
