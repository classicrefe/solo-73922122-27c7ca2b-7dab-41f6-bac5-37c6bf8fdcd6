# 河道水面瓦片更正 API

河道巡测中更正一片水面瓦片的后端服务。基于 **FastAPI + SQLite**，凭**批号、整数瓦片行列号、
16×16 二值水面掩膜**新增或替换一片瓦片，并按**四邻接**重新划分水面斑块（连通可跨瓦片），
只增量重算受影响斑块，所有更正带版本号与回执。

## 业务规则

- **瓦片**：固定 16×16，掩膜为二维 0/1 数组（1=水面）。瓦片行列号为任意整数（含负数）。
- **全局像元坐标**：`全局行 = 瓦片行×16 + 片内行`，列同理；负瓦片按 floor 除法归属。
- **斑块**：水像元按**四邻接**（上下左右，不含对角）划分连通分量，连通可跨瓦片。
- **斑块编号**：取斑块内最小的全局像元坐标，**先比行再比列**，形如 `(15,0)`。
- **增量重算**：每次更正只重算
  1. 旧掩膜落在目标瓦片内的斑块；
  2. 像元与目标瓦片边界四邻接的斑块（可能与新水面合并）。

  原斑块被断开后，落在其他瓦片上的残部会以自身最小像元重新编号并出现在回执中。
  未受影响的斑块保持原编号、原形状，不进回执、不改库。
- **版本与回执**：每次成功更正全局版本号 +1，回执记录 `parent_version`（入仓时的上一版）、
  作废斑块编号 `superseded_patch_ids`，以及新增/改形斑块 `changed_patches`（含完整全局像元坐标）。
- **幂等**：批号是更正操作的幂等键。
  - 同批号 + 同内容（瓦片行列 + 掩膜指纹一致）重传 → 返回**原回执**（HTTP 200，标记
    `retransmitted=true`），不产生新版本；
  - 同批号 + 异内容 → **拒收**（HTTP 409），库保持最近成功版。
- **并发**：写操作在单事务 `BEGIN IMMEDIATE` 内完成，进程内加锁串行化；每份回执的
  `parent_version` 以入仓时的上一版为准。
- **失败安全**：任何写库异常整体回滚，下游始终能读到最近一次成功版本与完整斑块清单。

## 接口

启动后访问交互式文档：<http://localhost:8000/docs>

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/corrections` | 新增/替换一片瓦片，返回收据（新更正 201，同内容重传 200，异内容 409） |
| GET  | `/corrections/{batch_id}` | 按批号查历史回执 |
| GET  | `/patches` | 最新完整斑块清单；可用 `tile_row`/`tile_col` 过滤（跨片斑块仍返回完整像元） |
| GET  | `/tiles` | 已入库瓦片清单（含写入批号、版本、水面像元数） |
| GET  | `/health` | 健康检查与当前版本 |

### 请求示例

```bash
# 16x16 掩膜，这里用简短片段示意（实际需 16 行、每行 16 个 0/1）
curl -X POST http://localhost:8000/corrections \
  -H "Content-Type: application/json" \
  -d '{
    "batch_id": "b-2026-0001",
    "tile_row": 0,
    "tile_col": 0,
    "mask": [
      [1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
      [1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]
      /* ...共 16 行... */
    ]
  }'
```

### 回执示例

```json
{
  "version": 2,
  "parent_version": 1,
  "batch_id": "b-2026-0002",
  "tile_row": 1,
  "tile_col": 0,
  "superseded_patch_ids": ["(15,0)"],
  "changed_patches": [
    {
      "patch_id": "(15,0)",
      "min_row": 15,
      "min_col": 0,
      "pixel_count": 4,
      "pixels": [[15,0],[15,1],[16,0],[16,1]],
      "tiles": [[0,0],[1,0]]
    }
  ],
  "retransmitted": false
}
```

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

SQLite 数据持久化在命名卷 `water-data`（容器内 `/data`）。

### 本地直接运行

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
WATER_DB_PATH=./water.db uvicorn app.main:app --reload
```

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest
```

测试覆盖：掩膜编解码、负坐标瓦片、四邻接连通、跨片合并、斑块拆分（残部落其他瓦片）、
增量范围（远处斑块不动）、幂等重传/异内容拒收、并发更正版本链、写库失败回滚、HTTP 层校验。

## 目录结构

```
app/
  config.py    # 环境变量配置
  schemas.py   # Pydantic 请求/响应模型与内容指纹
  db.py        # SQLite 表结构、增量斑块重算、事务、幂等、查询
  main.py      # FastAPI 路由
tests/         # pytest 测试
Dockerfile
docker-compose.yml
```

## 部署说明

默认单 uvicorn worker，配合进程内锁保证并发更正严格串行。若需多 worker/多进程扩容，
应让多个容器共享同一存储卷，由 SQLite 的 `BEGIN IMMEDIATE` 写锁与 `busy_timeout` 兜底串行化；
超高并发写入场景建议改用 C/S 型数据库。
