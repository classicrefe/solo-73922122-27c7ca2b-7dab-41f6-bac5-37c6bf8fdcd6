"""SQLite 数据层：瓦片存储、增量斑块重算、版本与幂等回执。

设计要点
--------
* 每片 16x16 二值掩膜以 32 字节位掩码入库（256 bit）。
* 像元表只存水面像元（值=1），全局坐标 (row, col) 唯一。
* 斑块编号 = 斑块内最小全局像元坐标，先比行再比列，形如 "(r,c)"。
* 每次更正只重算受影响斑块：
  1) 旧掩膜落在目标瓦片内的斑块；
  2) 像元与目标瓦片边界四邻接的斑块（可能与新水面合并）。
* 全部写操作在单个 BEGIN IMMEDIATE 事务内完成，失败整体回滚，
  因此失败后下游仍能读到最近一次成功版本。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from .config import TILE_SIZE
from .geometry import pixels_to_wkt
from .schemas import (
    CorrectionReceipt,
    CorrectionRequest,
    Patch,
    PublicationManifest,
    PublishedPatch,
    PublishedPatchResult,
    PublishReceipt,
)

# ---------------------------------------------------------------------------
# 进程内串行锁（单 worker 下保证并发更正严格串行；多 worker 时再由
# SQLite 的 BEGIN IMMEDIATE 写锁兜底）。
# ---------------------------------------------------------------------------
_write_lock = threading.RLock()


class IdempotencyConflict(Exception):
    """同批号、异内容重传。"""

    def __init__(self, batch_id: str):
        self.batch_id = batch_id
        super().__init__(f"批号 {batch_id!r} 已用于不同内容的更正，请求被拒收")


class PublishConflict(Exception):
    """发布时源版号已变化（转换期间有新更正入库，或客户端版号过期）。"""

    def __init__(self, source_version: int, current_version: int):
        self.source_version = source_version
        self.current_version = current_version
        super().__init__(
            f"源版号 {source_version} 已失效（当前版号 {current_version}），"
            "请按最新版号重新发布"
        )


# ---------------------------------------------------------------------------
# 坐标 / 掩膜编解码
# ---------------------------------------------------------------------------
def encode_mask(mask: list[list[int]]) -> bytes:
    """16x16 的 0/1 掩膜 -> 32 字节位掩码，bit 序 = row*16+col（高位在前）。"""
    out = bytearray(TILE_SIZE * TILE_SIZE // 8)
    for r in range(TILE_SIZE):
        for c in range(TILE_SIZE):
            if mask[r][c]:
                idx = r * TILE_SIZE + c
                out[idx >> 3] |= 0x80 >> (idx & 7)
    return bytes(out)


def decode_mask(blob: bytes) -> list[list[int]]:
    mask = [[0] * TILE_SIZE for _ in range(TILE_SIZE)]
    for r in range(TILE_SIZE):
        for c in range(TILE_SIZE):
            idx = r * TILE_SIZE + c
            if blob[idx >> 3] & (0x80 >> (idx & 7)):
                mask[r][c] = 1
    return mask


def tile_origin(tile_row: int, tile_col: int) -> tuple[int, int]:
    return tile_row * TILE_SIZE, tile_col * TILE_SIZE


def pixel_tile(row: int, col: int) -> tuple[int, int]:
    """全局像元坐标 -> 所属瓦片行列（Python floor 除法，负坐标同样正确）。"""
    return row // TILE_SIZE, col // TILE_SIZE


def make_patch_id(min_row: int, min_col: int) -> str:
    return f"({min_row},{min_col})"


def parse_patch_id(patch_id: str) -> tuple[int, int]:
    r, c = patch_id.strip("()").split(",")
    return int(r), int(c)


# ---------------------------------------------------------------------------
# 连接与建表
# ---------------------------------------------------------------------------
def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 单连接配合模块级 RLock 跨线程使用；多进程部署时由 BEGIN IMMEDIATE 写锁兜底。
    conn = sqlite3.connect(
        str(db_path), timeout=30, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_version INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO version(id, current_version) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS tiles (
    tile_row INTEGER NOT NULL,
    tile_col INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    version  INTEGER NOT NULL,
    mask     BLOB NOT NULL,
    PRIMARY KEY (tile_row, tile_col)
);

CREATE TABLE IF NOT EXISTS pixels (
    gr  INTEGER NOT NULL,
    gc  INTEGER NOT NULL,
    tile_row INTEGER NOT NULL,
    tile_col INTEGER NOT NULL,
    PRIMARY KEY (gr, gc)
);
CREATE INDEX IF NOT EXISTS idx_pixels_tile ON pixels(tile_row, tile_col);

CREATE TABLE IF NOT EXISTS patches (
    patch_id TEXT PRIMARY KEY,
    min_row  INTEGER NOT NULL,
    min_col  INTEGER NOT NULL,
    version  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS patch_pixels (
    patch_id TEXT NOT NULL REFERENCES patches(patch_id) ON DELETE CASCADE,
    gr INTEGER NOT NULL,
    gc INTEGER NOT NULL,
    PRIMARY KEY (patch_id, gr, gc)
);
CREATE INDEX IF NOT EXISTS idx_pp_pixel ON patch_pixels(gr, gc);

CREATE TABLE IF NOT EXISTS receipts (
    batch_id TEXT PRIMARY KEY,
    version  INTEGER NOT NULL,
    request_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

-- 发布成果：release_no=0 / source_version=-1 表示尚未发布。
CREATE TABLE IF NOT EXISTS publication_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    release_no INTEGER NOT NULL DEFAULT 0,
    source_version INTEGER NOT NULL DEFAULT -1
);
INSERT OR IGNORE INTO publication_meta(id, release_no, source_version) VALUES (1, 0, -1);

-- 当前生效的斑块面清单（整版切换：发布事务内整体删除并重写）。
CREATE TABLE IF NOT EXISTS published_patches (
    patch_id TEXT PRIMARY KEY,
    wkt TEXT NOT NULL,
    pixel_hash TEXT NOT NULL,
    pixel_count INTEGER NOT NULL,
    release_no INTEGER NOT NULL,
    source_version INTEGER NOT NULL
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT current_version FROM version WHERE id=1").fetchone()[0])


# ---------------------------------------------------------------------------
# 四邻接连通分量（内存洪水填充）
# ---------------------------------------------------------------------------
_NEIGHBORS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def label_components(candidates: set[tuple[int, int]]) -> list[set[tuple[int, int]]]:
    """对候选像元集合做四邻接连通分量划分，返回分量集合列表（无序）。"""
    remaining = set(candidates)
    components: list[set[tuple[int, int]]] = []
    while remaining:
        seed = next(iter(remaining))
        stack = [seed]
        remaining.discard(seed)
        comp: set[tuple[int, int]] = set()
        while stack:
            r, c = stack.pop()
            comp.add((r, c))
            for dr, dc in _NEIGHBORS:
                nb = (r + dr, c + dc)
                if nb in remaining:
                    remaining.discard(nb)
                    stack.append(nb)
        components.append(comp)
    return components


def build_patch(comp: Iterable[tuple[int, int]]) -> Patch:
    pixels = sorted(comp)  # 先比行再比列
    min_r, min_c = pixels[0]
    tiles = sorted({pixel_tile(r, c) for r, c in pixels})
    return Patch(
        patch_id=make_patch_id(min_r, min_c),
        min_row=min_r,
        min_col=min_c,
        pixel_count=len(pixels),
        pixels=[[r, c] for r, c in pixels],
        tiles=[[tr, tc] for tr, tc in tiles],
    )


# ---------------------------------------------------------------------------
# 更正主流程
# ---------------------------------------------------------------------------
def apply_correction(conn: sqlite3.Connection, req: CorrectionRequest) -> CorrectionReceipt:
    """在单个事务内新增/替换一片瓦片并增量重算受影响斑块。

    * 同批号 + 同指纹：原样返回历史回执（标记 retransmitted，不产生新版本）。
    * 同批号 + 异指纹：抛 IdempotencyConflict（HTTP 409），事务回滚。
    """
    fingerprint = req.fingerprint()
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            parent_version = current_version(conn)

            # ---- 幂等判定 ----
            row = conn.execute(
                "SELECT payload_json FROM receipts WHERE batch_id=?", (req.batch_id,)
            ).fetchone()
            if row is not None:
                stored = json.loads(row["payload_json"])
                if stored.get("request_fingerprint") == fingerprint:
                    conn.execute("COMMIT")
                    receipt = CorrectionReceipt(**stored["receipt"])
                    # 原样返回历史回执，仅标记本次为重传（不改库、不增版本）
                    return receipt.model_copy(update={"retransmitted": True})
                # 异内容：拒收，交由统一回滚处理
                raise IdempotencyConflict(req.batch_id)

            tr, tc = req.tile_row, req.tile_col
            r0, c0 = tile_origin(tr, tc)
            r1, c1 = r0 + TILE_SIZE, c0 + TILE_SIZE

            # ---- 载入当前水面像元与旧斑块映射 ----
            water: set[tuple[int, int]] = {
                (r["gr"], r["gc"]) for r in conn.execute("SELECT gr, gc FROM pixels")
            }
            patch_pixels: dict[str, set[tuple[int, int]]] = {}
            for r in conn.execute("SELECT patch_id, gr, gc FROM patch_pixels"):
                patch_pixels.setdefault(r["patch_id"], set()).add((r["gr"], r["gc"]))

            old_tile = conn.execute(
                "SELECT 1 FROM tiles WHERE tile_row=? AND tile_col=?", (tr, tc)
            ).fetchone()

            # 受影响集合 A：占有目标瓦片内像元的旧斑块
            affected: set[str] = set()
            for pid, pxs in patch_pixels.items():
                if any(r0 <= r < r1 and c0 <= c < c1 for r, c in pxs):
                    affected.add(pid)

            # 受影响集合 B：与目标瓦片边界四邻接的斑块（合并候选）。
            # 只统计片外像元，片内邻接已由集合 A 覆盖。
            for pid, pxs in patch_pixels.items():
                if pid in affected:
                    continue
                for r, c in pxs:
                    if r0 <= r < r1 and c0 <= c < c1:
                        continue
                    touches = (
                        (r == r0 - 1 and c0 <= c < c1)
                        or (r == r1 and c0 <= c < c1)
                        or (c == c0 - 1 and r0 <= r < r1)
                        or (c == c1 and r0 <= r < r1)
                    )
                    if touches:
                        affected.add(pid)
                        break

            # ---- 替换瓦片像元 ----
            water = {p for p in water if not (r0 <= p[0] < r1 and c0 <= p[1] < c1)}
            new_pixels: set[tuple[int, int]] = set()
            for lr in range(TILE_SIZE):
                for lc in range(TILE_SIZE):
                    if req.mask[lr][lc]:
                        new_pixels.add((r0 + lr, c0 + lc))
            water |= new_pixels

            # ---- 候选像元（供洪水填充）----
            # 受影响旧斑块中仍存活的像元 ∪ 新瓦片水面，再与当前水面求交，
            # 保证被本次抹除的旧像元不会被连进新斑块。
            # 而 source_pids 直接查重算前快照 patch_pixels，
            # 因此合并/拆分的来源追溯不受此过滤影响。
            candidates: set[tuple[int, int]] = set(new_pixels)
            for pid in affected:
                candidates |= patch_pixels.get(pid, set())
            candidates &= water

            # ---- 四邻接划分，构造新斑块 ----
            new_patches = [build_patch(comp) for comp in label_components(candidates)]

            # 像元 -> 所属旧斑块的反查表，把来源追溯降为 O(像元)
            pixel_owner: dict[tuple[int, int], str] = {}
            for pid, pxs in patch_pixels.items():
                for px in pxs:
                    pixel_owner[px] = pid

            def source_pids(p: Patch) -> set[str]:
                """新斑块的每个像元来自哪些旧斑块。"""
                return {
                    pixel_owner[(r, c)]
                    for r, c in p.pixels
                    if (r, c) in pixel_owner
                }

            # 对比重算前后：拆分/合并/改形/不变/新生
            superseded_set: set[str] = set()
            changed: list[Patch] = []
            unchanged_ids: set[str] = set()
            for p in new_patches:
                sources = source_pids(p)
                new_pixel_set = {tuple(x) for x in p.pixels}
                same_id_old = patch_pixels.get(p.patch_id)
                if not sources:
                    # 像元全部来自新水面；同编号旧斑块存在则被替换，否则是全新斑块
                    if same_id_old is not None:
                        superseded_set.add(p.patch_id)
                    changed.append(p)
                elif (
                    len(sources) == 1
                    and p.patch_id in sources
                    and same_id_old is not None
                    and new_pixel_set == same_id_old
                ):
                    # 同编号且像元集合完全一致：未变化，保留原库行，不进回执
                    unchanged_ids.add(p.patch_id)
                else:
                    # 拆分、合并或改形：来源旧斑块作废，新斑块落库
                    superseded_set |= sources
                    changed.append(p)

            # 受影响但整体消失的旧斑块（如整片水面被抹除）同样作废
            surviving_sources: set[str] = set()
            for p in new_patches:
                surviving_sources |= source_pids(p)
            superseded_set |= affected - surviving_sources

            superseded = sorted(superseded_set, key=parse_patch_id)
            changed.sort(key=lambda p: (p.min_row, p.min_col))
            to_insert = [p for p in new_patches if p.patch_id not in unchanged_ids]

            # ---- 持久化（同一事务）----
            if superseded:
                conn.executemany(
                    "DELETE FROM patch_pixels WHERE patch_id=?",
                    [(pid,) for pid in superseded],
                )
                conn.executemany(
                    "DELETE FROM patches WHERE patch_id=?",
                    [(pid,) for pid in superseded],
                )

            for p in to_insert:
                conn.execute(
                    "INSERT INTO patches(patch_id, min_row, min_col, version) VALUES (?,?,?,?)",
                    (p.patch_id, p.min_row, p.min_col, parent_version + 1),
                )
                conn.executemany(
                    "INSERT INTO patch_pixels(patch_id, gr, gc) VALUES (?,?,?)",
                    [(p.patch_id, r, c) for r, c in p.pixels],
                )

            conn.execute(
                "DELETE FROM pixels WHERE tile_row=? AND tile_col=?", (tr, tc)
            )
            conn.executemany(
                "INSERT INTO pixels(gr, gc, tile_row, tile_col) VALUES (?,?,?,?)",
                [(r, c, tr, tc) for r, c in sorted(new_pixels)],
            )
            if old_tile is not None:
                conn.execute(
                    "UPDATE tiles SET batch_id=?, version=?, mask=? "
                    "WHERE tile_row=? AND tile_col=?",
                    (req.batch_id, parent_version + 1, encode_mask(req.mask), tr, tc),
                )
            else:
                conn.execute(
                    "INSERT INTO tiles(tile_row, tile_col, batch_id, version, mask) "
                    "VALUES (?,?,?,?,?)",
                    (tr, tc, req.batch_id, parent_version + 1, encode_mask(req.mask)),
                )

            new_version = parent_version + 1
            conn.execute(
                "UPDATE version SET current_version=? WHERE id=1", (new_version,)
            )

            receipt = CorrectionReceipt(
                version=new_version,
                parent_version=parent_version,
                batch_id=req.batch_id,
                tile_row=tr,
                tile_col=tc,
                superseded_patch_ids=superseded,
                changed_patches=changed,
                retransmitted=False,
            )
            conn.execute(
                "INSERT INTO receipts(batch_id, version, request_fingerprint, payload_json) "
                "VALUES (?,?,?,?)",
                (
                    req.batch_id,
                    new_version,
                    fingerprint,
                    json.dumps(
                        {"request_fingerprint": fingerprint, "receipt": receipt.model_dump()},
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.execute("COMMIT")
            return receipt
        except Exception:
            # 任何异常（含 IdempotencyConflict 与写库失败）整体回滚，
            # 库始终停留在最近一次成功版本。
            conn.execute("ROLLBACK")
            raise


def get_receipt(conn: sqlite3.Connection, batch_id: str) -> CorrectionReceipt | None:
    row = conn.execute(
        "SELECT payload_json FROM receipts WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if row is None:
        return None
    return CorrectionReceipt(**json.loads(row["payload_json"])["receipt"])


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def list_patches(
    conn: sqlite3.Connection, tile_row: int | None = None, tile_col: int | None = None
) -> tuple[int, list[Patch]]:
    """返回 (当前版本, 完整斑块清单)。可按瓦片过滤（含跨片斑块的完整像元）。"""
    version = current_version(conn)
    rows = conn.execute(
        "SELECT patch_id, min_row, min_col FROM patches ORDER BY min_row, min_col"
    ).fetchall()
    patches: list[Patch] = []
    for row in rows:
        pid = row["patch_id"]
        pxs = sorted(
            (r["gr"], r["gc"])
            for r in conn.execute(
                "SELECT gr, gc FROM patch_pixels WHERE patch_id=? ORDER BY gr, gc",
                (pid,),
            )
        )
        if tile_row is not None or tile_col is not None:
            def on_tile(rc: tuple[int, int]) -> bool:
                ttr, ttc = pixel_tile(rc[0], rc[1])
                ok = True
                if tile_row is not None:
                    ok = ok and ttr == tile_row
                if tile_col is not None:
                    ok = ok and ttc == tile_col
                return ok

            if not any(on_tile(rc) for rc in pxs):
                continue
        tiles = sorted({pixel_tile(r, c) for r, c in pxs})
        patches.append(
            Patch(
                patch_id=pid,
                min_row=row["min_row"],
                min_col=row["min_col"],
                pixel_count=len(pxs),
                pixels=[[r, c] for r, c in pxs],
                tiles=[[a, b] for a, b in tiles],
            )
        )
    return version, patches


def list_tiles(conn: sqlite3.Connection) -> tuple[int, list[dict]]:
    version = current_version(conn)
    rows = conn.execute(
        """
        SELECT t.tile_row AS tile_row, t.tile_col AS tile_col,
               t.batch_id AS batch_id, t.version AS version,
               (SELECT COUNT(*) FROM pixels p
                WHERE p.tile_row=t.tile_row AND p.tile_col=t.tile_col)
                    AS water_pixel_count
        FROM tiles t
        ORDER BY t.tile_row, t.tile_col
        """
    ).fetchall()
    return version, [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 发布：斑块 -> WKT 面清单（三段式：快照 -> 转换 -> 整版提交）
# ---------------------------------------------------------------------------
def pixel_set_hash(pixels: set[tuple[int, int]]) -> str:
    """像元集合指纹：判定"与上次发布相同"以沿用原边界。"""
    raw = ";".join(f"{r},{c}" for r, c in sorted(pixels))
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def snapshot_for_publish(
    db_path: Path, source_version: int
) -> tuple[list[tuple[str, set[tuple[int, int]]]], dict[str, tuple[str, str]]]:
    """独立只读连接打一致性快照：校验源版号，取出该版全部斑块像元与上次发布清单。

    返回 (按编号排序的 [(patch_id, 像元集合)], {patch_id: (pixel_hash, wkt)})。
    快照事务不持有写锁，转换期间更正仍可正常入库；若快照时版号已变，
    立即抛 PublishConflict。
    """
    snap = connect(db_path)
    try:
        snap.execute("BEGIN")
        try:
            ver = current_version(snap)
            if ver != source_version:
                raise PublishConflict(source_version, ver)
            patches: dict[str, set[tuple[int, int]]] = {}
            for r in snap.execute("SELECT patch_id, gr, gc FROM patch_pixels"):
                patches.setdefault(r["patch_id"], set()).add((r["gr"], r["gc"]))
            old = {
                r["patch_id"]: (r["pixel_hash"], r["wkt"])
                for r in snap.execute(
                    "SELECT patch_id, pixel_hash, wkt FROM published_patches"
                )
            }
            snap.execute("COMMIT")
        except Exception:
            snap.execute("ROLLBACK")
            raise
    finally:
        snap.close()
    ordered = sorted(patches.items(), key=lambda kv: parse_patch_id(kv[0]))
    return ordered, old


def build_manifest(
    patches: list[tuple[str, set[tuple[int, int]]]],
    old_manifest: dict[str, tuple[str, str]],
) -> tuple[list[dict], int, int, int]:
    """纯函数转换：像元集合与上次发布相同的斑块沿用原边界，否则重新转换。

    返回 (清单条目, 重新转换数, 沿用数, 移除数)。不触碰数据库，
    因此转换期间瓦片更正可并发入库。
    """
    entries: list[dict] = []
    converted = reused = 0
    for pid, pxs in patches:
        h = pixel_set_hash(pxs)
        old = old_manifest.get(pid)
        if old is not None and old[0] == h:
            # 像元集合与上次发布相同：沿用原边界
            wkt, flag = old[1], True
            reused += 1
        else:
            # 新斑块，或同号斑块像元有变化：重新转换
            wkt, flag = pixels_to_wkt(pxs), False
            converted += 1
        entries.append(
            {
                "patch_id": pid,
                "wkt": wkt,
                "pixel_hash": h,
                "pixel_count": len(pxs),
                "reused": flag,
            }
        )
    removed = len(set(old_manifest) - {pid for pid, _ in patches})
    return entries, converted, reused, removed


def commit_manifest(
    conn: sqlite3.Connection, source_version: int, entries: list[dict]
) -> int:
    """整版提交：单事务内复验源版号、清空旧清单、写入新清单。

    版号已变 -> PublishConflict 回滚；任何写库异常整体回滚，
    原发布成果保持可查。返回新发布序号。
    """
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ver = current_version(conn)
            if ver != source_version:
                raise PublishConflict(source_version, ver)
            release_no = int(
                conn.execute(
                    "SELECT release_no FROM publication_meta WHERE id=1"
                ).fetchone()[0]
            ) + 1
            conn.execute("DELETE FROM published_patches")
            conn.executemany(
                "INSERT INTO published_patches"
                "(patch_id, wkt, pixel_hash, pixel_count, release_no, source_version)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (
                        e["patch_id"],
                        e["wkt"],
                        e["pixel_hash"],
                        e["pixel_count"],
                        release_no,
                        source_version,
                    )
                    for e in entries
                ],
            )
            conn.execute(
                "UPDATE publication_meta SET release_no=?, source_version=? WHERE id=1",
                (release_no, source_version),
            )
            conn.execute("COMMIT")
            return release_no
        except Exception:
            conn.execute("ROLLBACK")
            raise


def publish(
    conn: sqlite3.Connection, db_path: Path, source_version: int
) -> PublishReceipt:
    """发布主流程：快照 -> 转换（不持锁）-> 整版提交（复验版号）。

    转换或提交任一环节失败，已发布清单保持原样可查。
    """
    patches, old_manifest = snapshot_for_publish(db_path, source_version)
    entries, converted, reused, removed = build_manifest(patches, old_manifest)
    release_no = commit_manifest(conn, source_version, entries)
    return PublishReceipt(
        release_no=release_no,
        source_version=source_version,
        patch_count=len(entries),
        converted_count=converted,
        reused_count=reused,
        removed_count=removed,
        patches=[
            PublishedPatchResult(
                patch_id=e["patch_id"],
                wkt=e["wkt"],
                pixel_count=e["pixel_count"],
                reused=e["reused"],
            )
            for e in entries
        ],
    )


def get_current_manifest(conn: sqlite3.Connection) -> PublicationManifest | None:
    """下游查询：当前生效的斑块面清单。尚未发布时返回 None。"""
    meta = conn.execute(
        "SELECT release_no, source_version FROM publication_meta WHERE id=1"
    ).fetchone()
    if meta["release_no"] == 0:
        return None
    rows = conn.execute(
        "SELECT patch_id, wkt, pixel_count FROM published_patches ORDER BY patch_id"
    ).fetchall()
    return PublicationManifest(
        release_no=meta["release_no"],
        source_version=meta["source_version"],
        patch_count=len(rows),
        patches=[
            PublishedPatch(
                patch_id=r["patch_id"], wkt=r["wkt"], pixel_count=r["pixel_count"]
            )
            for r in rows
        ],
    )
