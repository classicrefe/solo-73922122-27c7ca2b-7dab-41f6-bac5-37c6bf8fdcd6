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

## 发布：斑块 -> WKT 面清单（GIS 图层）

制图侧可把某一版瓦片库的斑块整版发布为 WKT 面清单，供 GIS 当面图层使用。

- **坐标约定**：像元为单位方格，左上角坐标取 **(全局列, 负全局行)**，即 x=全局列、
  y=-全局行，方格覆盖 x∈[col, col+1]、y∈[-row-1, -row]。外环逆时针、内孔顺时针，
  输出 `POLYGON`（含内孔环）；仅对角相接的输入会拆成 `MULTIPOLYGON`（防御性分支，
  四邻接斑块不会触发）。覆盖范围与水像元并集**精确一致**，内孔保留。
- **三段式发布**：快照（独立只读连接，校验源版号）→ 转换（不持写锁，
  **期间瓦片更正照常入库**）→ 整版提交（单事务复验版号、整体换清单）。
- **增量转换**：像元集合与上次发布相同的斑块**沿用原边界**；同号但像元有变化的
  斑块重新转换；已消失的斑块从新清单移除。回执给出
  `converted_count` / `reused_count` / `removed_count`。
- **冲突**：提交时源版号已变（转换期间来了新更正，或版号本就过期）→ **HTTP 409**，
  原清单不动，按最新版号重新发布即可。
- **失败安全**：转换或提交任一环节失败，已发布清单保持原样可查。
- **查询**：`GET /publications/current` 返回当前生效清单的发布序号、源版号、
  各斑块编号与 WKT 边界；尚未发布时返回 404。

## 接口

启动后访问交互式文档：<http://localhost:8000/docs>

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/corrections` | 新增/替换一片瓦片，返回收据（新更正 201，同内容重传 200，异内容 409） |
| GET  | `/corrections/{batch_id}` | 按批号查历史回执 |
| GET  | `/patches` | 最新完整斑块清单；可用 `tile_row`/`tile_col` 过滤（跨片斑块仍返回完整像元） |
| GET  | `/tiles` | 已入库瓦片清单（含写入批号、版本、水面像元数） |
| POST | `/publications` | 发布：把 `source_version` 版的全部斑块转成 WKT 面清单并整版生效（201；版号已变 409） |
| GET  | `/publications/current` | 当前已发布的斑块面清单（编号、WKT 边界、源版号；未发布 404） |
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

### 发布示例

```bash
# 1. 看当前瓦片库版号
curl -s http://localhost:8000/health        # {"status":"ok","version":2}

# 2. 按该版号发布
curl -X POST http://localhost:8000/publications \
  -H "Content-Type: application/json" \
  -d '{"source_version": 2}'

# 3. 下游查询当前生效的面清单（编号、WKT 边界、源版号）
curl -s http://localhost:8000/publications/current
```

发布回执示例：

```json
{
  "release_no": 1,
  "source_version": 2,
  "patch_count": 1,
  "converted_count": 1,
  "reused_count": 0,
  "removed_count": 0,
  "patches": [
    {
      "patch_id": "(15,0)",
      "wkt": "POLYGON((0 -15, 2 -15, 2 -17, 0 -17, 0 -15))",
      "pixel_count": 4,
      "reused": false
    }
  ]
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
增量范围（远处斑块不动）、幂等重传/异内容拒收、并发更正版本链、写库失败回滚、HTTP 层校验；
WKT 面转换（坐标映射、覆盖一致性、内孔、对角掐点、确定性）、发布三段式（增量沿用、
同号改形重转、消失移除、版号冲突、转换/提交失败回滚、发布与更正并发）。

## 目录结构

```
app/
  config.py    # 环境变量配置
  schemas.py   # Pydantic 请求/响应模型与内容指纹
  geometry.py  # 像元集合 -> WKT 面（边界缝合、内孔、掐点拆分）
  db.py        # SQLite 表结构、增量斑块重算、事务、幂等、发布三段式、查询
  main.py      # FastAPI 路由
tests/         # pytest 测试
Dockerfile
docker-compose.yml
```

## 部署说明

默认单 uvicorn worker，配合进程内锁保证并发更正严格串行。若需多 worker/多进程扩容，
应让多个容器共享同一存储卷，由 SQLite 的 `BEGIN IMMEDIATE` 写锁与 `busy_timeout` 兜底串行化；
超高并发写入场景建议改用 C/S 型数据库。
