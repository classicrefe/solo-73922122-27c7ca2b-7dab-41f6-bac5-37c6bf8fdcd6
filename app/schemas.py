"""请求 / 响应的 Pydantic 模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .config import TILE_SIZE


class CorrectionRequest(BaseModel):
    """一片瓦片的更正请求。

    - batch_id: 批号，同时作为更正操作的幂等键
    - tile_row / tile_col: 整数瓦片行列号（可为负）
    - mask: 16x16 二值水面掩膜，1=水面，0=非水面
    """

    batch_id: str = Field(..., min_length=1, max_length=128, description="批号（幂等键）")
    tile_row: int = Field(..., description="瓦片行号（整数，可为负）")
    tile_col: int = Field(..., description="瓦片列号（整数，可为负）")
    mask: list[list[int]] = Field(..., description=f"{TILE_SIZE}x{TILE_SIZE} 二值水面掩膜，1=水面")

    @field_validator("batch_id")
    @classmethod
    def _batch_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("batch_id 不能为空白")
        return v

    @field_validator("mask")
    @classmethod
    def _check_mask(cls, v: list[list[int]]) -> list[list[int]]:
        if len(v) != TILE_SIZE:
            raise ValueError(f"mask 必须恰好 {TILE_SIZE} 行，收到 {len(v)} 行")
        for i, row in enumerate(v):
            if len(row) != TILE_SIZE:
                raise ValueError(f"mask 第 {i} 行必须恰好 {TILE_SIZE} 列，收到 {len(row)} 列")
            for j, cell in enumerate(row):
                if cell not in (0, 1):
                    raise ValueError(f"mask 只能含 0/1，位置 ({i},{j}) 收到 {cell!r}")
        return v

    def fingerprint(self) -> str:
        """内容指纹：同批号重传时据此判定内容是否一致。"""
        import hashlib

        flat = "".join(str(c) for row in self.mask for c in row)
        raw = f"{self.tile_row}|{self.tile_col}|{flat}"
        return hashlib.sha256(raw.encode("ascii")).hexdigest()


class Patch(BaseModel):
    """一个水面斑块（四邻接连通分量）。"""

    patch_id: str = Field(..., description="斑块编号 = 最小全局像元坐标 (row,col)，先比行再比列")
    min_row: int
    min_col: int
    pixel_count: int = Field(..., description="斑块像元总数")
    pixels: list[list[int]] = Field(..., description="全部全局像元坐标 [row, col]，按 (行,列) 升序")
    tiles: list[list[int]] = Field(..., description="斑块跨越的瓦片行列号 [tile_row, tile_col]，升序去重")


class CorrectionReceipt(BaseModel):
    """带版本号的更正值执回执。"""

    version: int = Field(..., description="本次更正生效后的全局版本号")
    parent_version: int = Field(..., description="入仓时所依据的上一版版本号")
    batch_id: str
    tile_row: int
    tile_col: int
    superseded_patch_ids: list[str] = Field(
        default_factory=list, description="作废的斑块编号（被拆开/合并/改形的旧斑块）"
    )
    changed_patches: list[Patch] = Field(
        default_factory=list, description="新增或改形后的斑块（含完整全局像元坐标）"
    )
    retransmitted: bool = Field(
        default=False, description="True=同批号同内容重传，原样返回历史回执，未产生新版本"
    )


class PatchListResponse(BaseModel):
    """下游查询：最新完整斑块清单。"""

    version: int = Field(..., description="当前已生效的最新成功版本号（0=尚无更正）")
    patch_count: int
    patches: list[Patch]


class TileInfo(BaseModel):
    tile_row: int
    tile_col: int
    batch_id: str = Field(..., description="最近一次写入该片的批号")
    version: int = Field(..., description="最近一次写入时的版本号")
    water_pixel_count: int


class TileListResponse(BaseModel):
    version: int
    tile_count: int
    tiles: list[TileInfo]


class HealthResponse(BaseModel):
    status: str
    version: int
