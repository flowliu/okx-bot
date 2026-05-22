# OrbitAI — AI-driven grid trading bot for OKX
# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under MIT
#
# 单容器：webui (uvicorn) + bot (webui 通过 subprocess 起 main.py)
# 持久化数据走 /data（挂载 host 目录），不污染镜像。

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    APP_HOME=/app \
    DATA_DIR=/data

WORKDIR ${APP_HOME}

# 时区数据 + tini 做 PID 1 信号转发（让 SIGTERM 能正确停 uvicorn 与子进程）
RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata tini ca-certificates && \
    ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && \
    echo ${TZ} > /etc/timezone && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 先装依赖（缓存层）
COPY requirements.txt .
RUN pip install -r requirements.txt

# 复制代码
COPY *.py ./
COPY prompts/ ./prompts/
COPY static/ ./static/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 非 root 用户运行（更安全）
RUN useradd -u 1000 -ms /bin/bash orbit && \
    mkdir -p ${DATA_DIR} && \
    chown -R orbit:orbit ${APP_HOME} ${DATA_DIR}
USER orbit

EXPOSE 8765

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "webui:app", "--host", "0.0.0.0", "--port", "8765"]
