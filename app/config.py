"""运行期配置（全部可由环境变量覆盖）。"""

from __future__ import annotations

import os
from pathlib import Path

# SQLite 数据库文件位置。Docker Compose 中挂卷到 /data。
DB_PATH = Path(os.getenv("WATER_DB_PATH", "/data/water.db"))

# 瓦片固定 16x16，二值掩膜。
TILE_SIZE = 16

# 同批号异内容时返回的 HTTP 状态（409 Conflict）。
IDEMPOTENCY_CONFLICT_STATUS = 409
