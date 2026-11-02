# syntax=docker/dockerfile:1

# =============================================================================
# M5 §7 前端镜像：Next.js standalone 多阶段构建 + 同容器 nginx
#
# **构建上下文是 frontend/ 本身**，不是仓库根：
#   docker build -f docker/Dockerfile -t myflow-frontend .        # 在 frontend/ 里
#   compose 里写的是 context: ..（frontend/docker/ 的上一级）——见 docker-compose.yml。
# 理由：pnpm-lock.yaml 和 pnpm-workspace.yaml 都在 frontend/ 这一层，pnpm 在这里
# 就是一个完整的 workspace 根；把上下文放大到仓库根只会把 backend/、.venv 之类的
# 东西塞进构建缓存，还会让 pnpm 找不到自己的锁文件。
#
# 这个镜像里跑两个进程（见 CMD）：Next standalone server 监听 127.0.0.1:3000，
# nginx 监听 0.0.0.0:2026 对外。浏览器只需要认识一个同源端口（§7.3 的
# "发布出去的那个端口才是全部的外部攻击面"）。
# =============================================================================

# -----------------------------------------------------------------------------
# deps：只装依赖。这一层只依赖两个 manifest，源码改动不会让它失效。
# -----------------------------------------------------------------------------
FROM node:22-alpine AS deps
WORKDIR /app

# pnpm 版本要和仓库里的 pnpm-lock.yaml 对得上（lockfileVersion 9.0）。
# 本机用的是 pnpm 11.x，这里 pin 同一个 major，避免 lockfile settings 被重写。
RUN corepack enable && corepack prepare pnpm@11.22.0 --activate

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
# registry.npmjs.org 在当前网络下会超时。锁文件里的 tarball 地址改到镜像源，
# integrity 不变，frozen-lockfile 仍然有效。
RUN sed -i 's#https://registry.npmjs.org/#https://registry.npmmirror.com/#g' pnpm-lock.yaml \
    && pnpm config set fetch-retries 5 \
    && pnpm config set fetch-timeout 300000 \
    && pnpm install --frozen-lockfile

# -----------------------------------------------------------------------------
# builder：产出 .next/standalone（next.config.ts 里的 output: "standalone"）
# -----------------------------------------------------------------------------
FROM node:22-alpine AS builder
WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /app/node_modules ./node_modules
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./

# 这里**故意逐个列文件**而不是 COPY . .：上下文里可能有宿主机的 node_modules
# （Windows 上装出来的二进制），一把梭会连它一起打进镜像。
# 代价是新增顶层配置文件时要记得加一行——漏了会以 COPY 失败的形式立刻暴露，
# 不会静默产出一个跑不起来的镜像。
COPY next.config.ts tsconfig.json postcss.config.mjs ./
COPY src ./src

# Next 的 standalone 输出里不会带 public/，而 runner 阶段要 COPY 它。
# 仓库暂时没有 public/，先建空的，避免 COPY 直接失败。
RUN mkdir -p public && ./node_modules/.bin/next build

# -----------------------------------------------------------------------------
# runner：只带 standalone 产物 + nginx
# -----------------------------------------------------------------------------
FROM node:22-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    GATEWAY_UPSTREAM=myflow-gateway:8001

# nginx 只在这里当反向代理用，不做任何缓存/压缩之外的活。
RUN apk add --no-cache nginx \
    && addgroup -g 1001 -S nodejs \
    && adduser  -u 1001 -S nextjs -G nodejs \
    # nginx 以非 root 跑，临时目录必须可写且不能落在 /var/lib/nginx
    && mkdir -p /tmp/nginx/client-body /tmp/nginx/proxy /tmp/nginx/fastcgi \
                /tmp/nginx/uwsgi /tmp/nginx/scgi \
    && chown -R nextjs:nodejs /tmp/nginx /app

COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

# 完整替换 alpine 自带的 /etc/nginx/nginx.conf（§7.1 的 gzip 白名单、
# §7.2 的 SSE 开关都在里面）
COPY --chown=root:root docker/nginx.conf /etc/nginx/nginx.conf.template

USER nextjs

# 2026 是 nginx（对外），3000 只是容器内部的 Next server
EXPOSE 2026

# 先起 Next，等它就绪再让 nginx 开始接流量——否则镜像刚起来的那几秒
# 所有请求都是 502。wget 是 busybox 自带的；等不到也照常启动 nginx，
# 让错误以 502 的形式出现在访问日志里，而不是容器直接退不出去。
#
# 注意：这里没有用 tini/supervisord，nginx 是 PID 1，容器停止时后台的 node
# 拿不到 SIGTERM（docker 会在超时后 SIGKILL）。M9 要做优雅排空时再换 supervisor。
# HOSTNAME / PORT 在这里再显式写一遍：docker 会给容器注入自己的 HOSTNAME（容器 ID），
# 直接跑 `node server.js` 有绑到一个解析不了的名字上的风险。nginx 反代的是 127.0.0.1:3000。
CMD ["sh", "-c", "HOSTNAME=0.0.0.0 PORT=3000 node server.js & for i in $(seq 1 50); do wget -q -O /dev/null http://127.0.0.1:3000/ && break; sleep 0.2; done; case \"$GATEWAY_UPSTREAM\" in *[!A-Za-z0-9.:_-]*) echo \"invalid GATEWAY_UPSTREAM\" >&2; exit 1;; esac; sed \"s#__GATEWAY_UPSTREAM__#$GATEWAY_UPSTREAM#g\" /etc/nginx/nginx.conf.template > /tmp/nginx.conf && exec nginx -c /tmp/nginx.conf -g 'daemon off;'"]
