# syntax=docker/dockerfile:1
# 只构建 MyFlow 网关。构建上下文是仓库根：
#   docker build -f docker/backend.Dockerfile -t myflow-backend:local .
#
# config.yaml 与 .env 打进镜像的 /app。compose 再把仓库里的这两份只读挂进去，
# 改配置不用重新构建。文件名是 config.yaml，不是 config.yml。

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
    MYFLOW_CONFIG_PATH=/app/config.yaml

COPY pyproject.toml uv.lock README.md ./
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/packages/harness/pyproject.toml backend/packages/harness/pyproject.toml
COPY backend backend
COPY skills skills

# 根项目没有 extras。网关可选依赖在 backend / agentkit 上。
# --inexact 让第二次 sync 只补 OpenViking，不卸掉第一次装上的包。
# MCP SDK 已归入 contextforge extra（不再有独立的 mcp extra）。
RUN uv sync --frozen --no-dev --package backend \
        --extra postgres \
        --extra redis \
        --extra knowledge \
        --extra skills \
        --extra observability \
        --extra opensandbox \
        --extra contextforge \
    && uv sync --frozen --no-dev --inexact --package agentkit \
        --extra openviking \
    && uv pip install --python /app/.venv/bin/python "uvicorn[standard]>=0.32"

COPY config.yaml .env ./

EXPOSE 8001

CMD ["/app/.venv/bin/uvicorn", "backend.app.gateway.app:app", "--host", "0.0.0.0", "--port", "8001"]
