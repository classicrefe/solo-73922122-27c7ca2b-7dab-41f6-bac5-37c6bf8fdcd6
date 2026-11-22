#!/usr/bin/env bash
# 在装有 Docker 的环境里一键冒烟：构建、起服务、打一条更正、查斑块、再下线。
set -euo pipefail

cd "$(dirname "$0")"
docker compose up -d --build

# 等待健康检查通过
for _ in $(seq 1 30); do
  if curl -sf http://localhost:8000/health >/dev/null; then break; fi
  sleep 1
done

curl -s http://localhost:8000/health; echo

# 构造一个 16x16 掩膜（左上角 2x2 水面）并提交
python3 - <<'PY'
import json, urllib.request
mask = [[1 if r < 2 and c < 2 else 0 for c in range(16)] for r in range(16)]
body = json.dumps({"batch_id": "smoke", "tile_row": 0, "tile_col": 0, "mask": mask}).encode()
req = urllib.request.Request(
    "http://localhost:8000/corrections", data=body, method="POST",
    headers={"Content-Type": "application/json"})
print(json.dumps(json.loads(urllib.request.urlopen(req).read()), ensure_ascii=False, indent=2))
print(urllib.request.urlopen("http://localhost:8000/patches").read().decode())
PY

echo "Docs: http://localhost:8000/docs"
echo "完成后执行: docker compose down -v"
