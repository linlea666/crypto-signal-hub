FROM python:3.11-slim AS base

# 系统依赖（最小化层）
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl tini tzdata && \
    ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖文件，利用 Docker 缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 持久化数据和日志的挂载点
RUN mkdir -p /app/data/logs

VOLUME ["/app/data"]

ENV CSH_DOCKER=true \
    CSH_HOST=0.0.0.0 \
    CSH_PORT=8686 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

EXPOSE 8686

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8686/api/health || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["python", "main.py"]
