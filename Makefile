# MyFlow 操作入口。所有命令默认在仓库根执行，`make help` 查看全部目标。
#
# Docker 目标统一读取 $(ENV_FILE) 与 $(CONFIG_FILE)，不会自动修改已有配置。
# 可通过命令行覆盖变量，例如：
#   make up ENV_FILE=.env.prod
#   make eval-build EVAL_DATASET=agent-eval/explore EVAL_ARGS="--include-traces"
#   make eval-run EVAL_MODE=full

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

COMPOSE ?= docker compose
ENV_FILE ?= .env
CONFIG_FILE ?= config.yaml
ENV_TEMPLATE ?= .env.example
CONFIG_TEMPLATE ?= config.example.yaml

DEPS_COMPOSE := $(COMPOSE) -f docker/docker-compose.yml --env-file $(ENV_FILE)
BACKEND_COMPOSE := $(COMPOSE) -f docker/backend-compose.yml --env-file $(ENV_FILE)
FRONTEND_COMPOSE := $(COMPOSE) -f docker/frontend-compose.yml --env-file $(ENV_FILE)

BACKEND_PORT ?= 8001
FRONTEND_PORT ?= 2026
BACKEND_ARGS ?=

EVAL_ENV_FILE ?= evals/.env
EVAL_DATASET ?= agent-eval/regression/v1
EVAL_MODE ?= ci
EVAL_LIMIT ?= 100
EVAL_DRY_LIMIT ?= 5
EVAL_CONCURRENCY ?= 2
EVAL_RUN_NAME ?= local-$(shell date +%Y%m%d-%H%M)
EVAL_ARGS ?=

.DEFAULT_GOAL := help

.PHONY: help init install install-all check-config check-deploy check-eval-env \
	dev-backend dev-frontend up up-backend up-frontend deploy down restart build ps \
	logs-backend logs-frontend logs-deps health \
	test test-backend test-frontend test-evals \
	eval-install eval-install-llm \
	eval-build-dry eval-build eval-run-dry eval-run

help: ## 显示本帮助
	@printf 'MyFlow\n\nUsage: make <target> [VAR=value]\n\n'
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} /^[a-zA-Z0-9_.-]+:.*##/ {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf '\nEval defaults: dataset=%s mode=%s limit=%s run-name=%s\n' \
		'$(EVAL_DATASET)' '$(EVAL_MODE)' '$(EVAL_LIMIT)' '$(EVAL_RUN_NAME)'
	@printf 'More variables: see the top of Makefile; full guide: README.md.\n'

init: ## 从模板补齐本地配置（不覆盖已有文件）
	@test -f $(ENV_FILE) || cp $(ENV_TEMPLATE) $(ENV_FILE)
	@test -f $(CONFIG_FILE) || cp $(CONFIG_TEMPLATE) $(CONFIG_FILE)
	@test -f frontend/.env.local || cp frontend/.env.example frontend/.env.local
	@test -f $(EVAL_ENV_FILE) || cp evals/env.template $(EVAL_ENV_FILE)
	@printf '\nLocal config checked. Fill in real values:\n'
	@printf '  .env              DEEPSEEK_API_KEY / SANDBOX_HOST_ROOT / DATABASE_URL / LANGFUSE_* ...\n'
	@printf '  evals/.env        AGENT_API / AGENT_USERS / USER_ID_SALT / LANGFUSE_* / EVAL_JUDGE_*\n'
	@printf '  Existing files are never overwritten.\n'

install: ## 安装本地依赖（uv workspace + 前端）
	uv sync --all-packages
	cd frontend && pnpm install

install-all: install ## 在 install 基础上安装所有 Python optional extras
	uv sync --all-packages --all-extras

check-config:
	@test -f $(ENV_FILE) || { printf 'Missing %s: run "make init" and fill in real values.\n' '$(ENV_FILE)' >&2; exit 1; }
	@test -f $(CONFIG_FILE) || { printf 'Missing %s: run "make init" and fill in real values.\n' '$(CONFIG_FILE)' >&2; exit 1; }

check-deploy: check-config
	@command -v docker >/dev/null 2>&1 || { printf 'docker not found: install Docker Desktop / Docker Engine first.\n' >&2; exit 1; }
	@$(COMPOSE) version >/dev/null 2>&1 || { printf 'docker compose unavailable: install Compose v2.\n' >&2; exit 1; }
	@docker info >/dev/null 2>&1 || { printf 'Docker daemon is not running: start Docker first.\n' >&2; exit 1; }

dev-backend: check-config ## 本机启动网关（0.0.0.0:8001）
	MYFLOW_CONFIG_PATH=$(CONFIG_FILE) \
		uv run --env-file $(ENV_FILE) --with uvicorn \
		uvicorn backend.app.gateway.app:app --host 0.0.0.0 --port $(BACKEND_PORT) $(BACKEND_ARGS)

dev-frontend: ## 本机启动前端（0.0.0.0:3000）
	@test -f frontend/.env.local || { printf 'Missing frontend/.env.local: run "make init" first.\n' >&2; exit 1; }
	cd frontend && pnpm dev

up: check-deploy ## 构建并启动全部容器（依赖 + 网关 + 前端）
	$(DEPS_COMPOSE) up -d
	$(BACKEND_COMPOSE) up -d --build
	$(FRONTEND_COMPOSE) up -d --build
	@printf '\nStarted: frontend http://0.0.0.0:2026/workspace , gateway http://0.0.0.0:8001/docs\n'
	@printf 'Status: make ps    Health: make health    Logs: make logs-backend\n'

up-backend: check-deploy ## 构建并启动后端容器（先起依赖，再起网关）
	$(DEPS_COMPOSE) up -d
	$(BACKEND_COMPOSE) up -d --build
	@printf '\nStarted: gateway http://0.0.0.0:$(BACKEND_PORT)/docs\n'
	@printf 'Status: make ps    Health: make health    Logs: make logs-backend\n'

up-frontend: check-deploy ## 构建并启动前端容器
	$(FRONTEND_COMPOSE) up -d --build
	@printf '\nStarted: frontend http://0.0.0.0:$(FRONTEND_PORT)/workspace\n'
	@printf 'Status: make ps    Health: make health    Logs: make logs-frontend\n'

deploy: up ## up 的别名

down: check-deploy ## 停止并移除全部容器（保留数据卷）
	$(FRONTEND_COMPOSE) down
	$(BACKEND_COMPOSE) down
	$(DEPS_COMPOSE) down
	@printf 'All containers stopped; data volumes preserved.\n'

restart: ## 重建并重启全部容器
	$(MAKE) down
	$(MAKE) up

build: check-deploy ## 只构建网关和前端镜像，不启动
	docker build -f docker/backend.Dockerfile -t myflow-backend:local .
	docker build -f docker/frontend.Dockerfile -t myflow-frontend:local .

ps: check-deploy ## 查看全部容器状态
	@printf '\n[deps]\n'
	@$(DEPS_COMPOSE) ps
	@printf '\n[backend]\n'
	@$(BACKEND_COMPOSE) ps
	@printf '\n[frontend]\n'
	@$(FRONTEND_COMPOSE) ps

logs-backend: check-deploy ## 跟踪网关日志
	$(BACKEND_COMPOSE) logs -f --tail=100

logs-frontend: check-deploy ## 跟踪前端日志
	$(FRONTEND_COMPOSE) logs -f --tail=100

logs-deps: check-deploy ## 跟踪依赖服务日志
	$(DEPS_COMPOSE) logs -f --tail=100

health: ## 探测网关与前端 HTTP 状态
	@status=0; \
	for entry in "gateway http://0.0.0.0:$(BACKEND_PORT)/docs" "frontend http://0.0.0.0:$(FRONTEND_PORT)/workspace"; do \
		set -- $$entry; \
		code=$$(curl -sS -o /dev/null -w '%{http_code}' "$$2" 2>/dev/null || true); \
		printf '%-8s %s  %s\n' "$$1" "$$code" "$$2"; \
		if [ "$$code" != "200" ]; then status=1; fi; \
	done; \
	exit $$status

test: test-backend test-frontend test-evals ## 运行后端、前端、评测全部单测

test-backend: ## 运行后端单测
	cd backend && uv run pytest

test-frontend: ## 运行前端单测
	cd frontend && pnpm test

test-evals: ## 运行评测包单测
	uv run --project evals pytest evals/tests -v

eval-install: ## 安装评测依赖（规则指标所需）
	uv sync --project evals

eval-install-llm: ## 安装评测的 DeepEval 可选依赖
	uv sync --project evals --extra llm

check-eval-env:
	@test -f $(EVAL_ENV_FILE) || { printf 'Missing %s: run "make init" (or cp evals/env.template evals/.env) and fill in real values.\n' '$(EVAL_ENV_FILE)' >&2; exit 1; }

eval-build-dry: check-eval-env ## 试构建数据集（LLM 增强、limit 5、不写 Langfuse）
	uv run --project evals myflow-evals build \
		--env-file $(EVAL_ENV_FILE) \
		--dataset $(EVAL_DATASET) \
		--llm-synthesis \
		--limit $(EVAL_DRY_LIMIT) \
		--dry-run \
		$(EVAL_ARGS)

eval-build: check-eval-env ## 构建并写入数据集（LLM 增强，默认 limit 100）
	uv run --project evals myflow-evals build \
		--env-file $(EVAL_ENV_FILE) \
		--dataset $(EVAL_DATASET) \
		--llm-synthesis \
		--limit $(EVAL_LIMIT) \
		$(EVAL_ARGS)

eval-run-dry: check-eval-env ## 试跑实验（只读 Langfuse，不启动 Agent）
	uv run --project evals myflow-evals run \
		--env-file $(EVAL_ENV_FILE) \
		--dataset $(EVAL_DATASET) \
		--mode $(EVAL_MODE) \
		--dry-run \
		$(EVAL_ARGS)

eval-run: check-eval-env ## 运行评测实验（默认 ci 子集）
	uv run --project evals myflow-evals run \
		--env-file $(EVAL_ENV_FILE) \
		--dataset $(EVAL_DATASET) \
		--mode $(EVAL_MODE) \
		--run-name "$(EVAL_RUN_NAME)" \
		--max-concurrency $(EVAL_CONCURRENCY) \
		$(EVAL_ARGS)
