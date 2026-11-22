"""FastAPI 应用：河道水面瓦片更正 API。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import __version__
from .config import DB_PATH, IDEMPOTENCY_CONFLICT_STATUS, PUBLISH_CONFLICT_STATUS
from .db import (
    IdempotencyConflict,
    PublishConflict,
    apply_correction,
    connect,
    current_version,
    get_current_manifest,
    get_receipt,
    init_db,
    list_patches,
    list_tiles,
    publish,
)
from .schemas import (
    CorrectionReceipt,
    CorrectionRequest,
    HealthResponse,
    PatchListResponse,
    PublicationManifest,
    PublishReceipt,
    PublishRequest,
    TileListResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.conn = connect(DB_PATH)
    init_db(app.state.conn)
    yield
    app.state.conn.close()


app = FastAPI(
    title="河道水面瓦片更正 API",
    description=(
        "凭批号、整数瓦片行列和 16×16 二值水面掩膜新增/替换一片水面瓦片；"
        "按四邻接划分斑块（可跨片），斑块编号取最小全局像元坐标（先行后列），"
        "只增量重算受影响斑块，所有更正带版本号与回执。"
        "发布接口把指定版号的全部斑块转成 WKT 面清单（含内孔，"
        "像元为单位方格，左上角取 (全局列, 负全局行)），整版生效后供下游查询。"
    ),
    version=__version__,
    lifespan=lifespan,
)


def get_conn():
    return app.state.conn


@app.post(
    "/corrections",
    response_model=CorrectionReceipt,
    status_code=201,
    summary="新增或替换一片水面瓦片",
    responses={
        IDEMPOTENCY_CONFLICT_STATUS: {
            "description": "同批号已用于异内容更正，请求拒收（数据保持最近成功版）"
        }
    },
)
def post_correction(req: CorrectionRequest, conn=Depends(get_conn)):
    try:
        receipt = apply_correction(conn, req)
    except IdempotencyConflict as exc:
        raise HTTPException(
            status_code=IDEMPOTENCY_CONFLICT_STATUS,
            detail={
                "error": "idempotency_conflict",
                "batch_id": exc.batch_id,
                "message": str(exc),
            },
        )
    # 同内容重传不产生新版本，返回 200；新更正返回 201。
    if receipt.retransmitted:
        return JSONResponse(status_code=200, content=receipt.model_dump())
    return receipt


@app.get(
    "/corrections/{batch_id}",
    response_model=CorrectionReceipt,
    summary="按批号查询历史回执",
)
def get_correction(batch_id: str, conn=Depends(get_conn)):
    receipt = get_receipt(conn, batch_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"批号 {batch_id!r} 不存在")
    return receipt


@app.get(
    "/patches",
    response_model=PatchListResponse,
    summary="下游查询：最新完整斑块清单",
)
def get_patches(
    tile_row: int | None = Query(None, description="可选：只看与该瓦片行相交的斑块"),
    tile_col: int | None = Query(None, description="可选：只看与该瓦片列相交的斑块"),
    conn=Depends(get_conn),
):
    version, patches = list_patches(conn, tile_row, tile_col)
    return PatchListResponse(version=version, patch_count=len(patches), patches=patches)


@app.get("/tiles", response_model=TileListResponse, summary="已入库瓦片清单")
def get_tiles(conn=Depends(get_conn)):
    version, tiles = list_tiles(conn)
    return TileListResponse(version=version, tile_count=len(tiles), tiles=tiles)


@app.post(
    "/publications",
    response_model=PublishReceipt,
    status_code=201,
    summary="发布：把指定版号的全部斑块转成 WKT 面清单，整版生效",
    responses={
        PUBLISH_CONFLICT_STATUS: {
            "description": "源版号已变化（转换期间有新更正入库或版号过期），发布冲突"
        }
    },
)
def post_publication(req: PublishRequest, conn=Depends(get_conn)):
    try:
        return publish(conn, DB_PATH, req.source_version)
    except PublishConflict as exc:
        raise HTTPException(
            status_code=PUBLISH_CONFLICT_STATUS,
            detail={
                "error": "source_version_conflict",
                "source_version": exc.source_version,
                "current_version": exc.current_version,
                "message": str(exc),
            },
        )


@app.get(
    "/publications/current",
    response_model=PublicationManifest,
    summary="下游查询：当前已发布的斑块面清单（编号、WKT 边界、源版号）",
)
def get_publication_current(conn=Depends(get_conn)):
    manifest = get_current_manifest(conn)
    if manifest is None:
        raise HTTPException(status_code=404, detail="尚未发布任何斑块面清单")
    return manifest


@app.get("/health", response_model=HealthResponse, summary="健康检查")
def health(conn=Depends(get_conn)):
    return HealthResponse(status="ok", version=current_version(conn))
