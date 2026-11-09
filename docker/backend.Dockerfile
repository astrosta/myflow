# syntax=docker/dockerfile:1
# 只构建 MyFlow 网关。构建上下文是仓库根：
#   docker build -f docker/backend.Dockerfile -t myflow-backend:local .
#
# config.yaml 与 .env 打进镜像的 /app。compose 再把仓库里的这两份只读挂进去，
# 改配置不用重新构建。文件名是 config.yaml，不是 config.yml。
#
# 依赖层与源码层分开：改 backend/*.py 不应迫使 openai / pydantic-core 等重新下载。
# BuildKit cache mount 让即使 lock 变了，重叠的 wheel 也能从 /root/.cache/uv 复用。

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/

WORKDIR /app

# python:*-slim 已带 ca-certificates。若再跑 apt-get update，默认会打
# deb.debian.org，国内经常要数分钟；只有真缺系统包时才装，并换国内源：
#   sed -i -e 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' \
#          -e 's/security.debian.org/mirrors.tuna.tsinghua.edu.cn/g' \
#          /etc/apt/sources.list.d/debian.sources \
#   && apt-get update && apt-get install -y --no-install-recommends <pkg> \
#   && rm -rf /var/lib/apt/lists/*

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
    MYFLOW_CONFIG_PATH=/app/config.yaml

# 只拷依赖清单。workspace 成员源码稍后再进镜像。
COPY pyproject.toml uv.lock README.md ./
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/packages/harness/pyproject.toml backend/packages/harness/pyproject.toml

# 根项目没有 extras。网关可选依赖在 backend / agentkit 上。
# --no-install-workspace：只装第三方依赖，不装本地包，这一层可被 Docker 缓存。
# --inexact 让第二次 sync 只补 OpenViking，不卸掉第一次装上的包。
# MCP SDK / cryptography / jsonschema 是 agentkit 的核心依赖，不用 extra。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package backend \
        --extra postgres \
        --extra redis \
        --extra knowledge \
        --extra skills \
        --extra tool_search \
        --extra observability \
        --extra opensandbox \
    && uv sync --frozen --no-dev --inexact --no-install-workspace --package agentkit \
        --extra openviking

COPY backend backend
COPY skills skills

# 源码进镜像后再装本地 workspace 包；第三方 wheel 走上面的 cache mount。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package backend \
        --extra postgres \
        --extra redis \
        --extra knowledge \
        --extra skills \
        --extra tool_search \
        --extra observability \
        --extra opensandbox \
    && uv sync --frozen --no-dev --inexact --package agentkit \
        --extra openviking \
    && uv pip install --python /app/.venv/bin/python "uvicorn[standard]>=0.32"

COPY config.yaml .env ./

EXPOSE 8001

CMD ["/app/.venv/bin/uvicorn", "backend.app.gateway.app:app", "--host", "0.0.0.0", "--port", "8001"]
