# 河道水面瓦片更正 API
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    WATER_DB_PATH=/data/water.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 数据卷，SQLite 的 db / wal / shm 都落在这里
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单 worker：进程内 RLock 保证并发更正严格串行、parent_version 链可靠。
# 水平扩容请改为多容器 + 共享卷，由 SQLite BEGIN IMMEDIATE 写锁兜底。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
