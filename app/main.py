"""FastAPI 应用：河道水面瓦片更正 API。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import __version__
from .config import DB_PATH, IDEMPOTENCY_CONFLICT_STATUS
from .db import (
    IdempotencyConflict,
    apply_correction,
    connect,
    current_version,
    get_receipt,
    init_db,
    list_patches,
    list_tiles,
)
from .schemas import (
    CorrectionReceipt,
    CorrectionRequest,
    HealthResponse,
    PatchListResponse,
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


@app.get("/health", response_model=HealthResponse, summary="健康检查")
def health(conn=Depends(get_conn)):
    return HealthResponse(status="ok", version=current_version(conn))
