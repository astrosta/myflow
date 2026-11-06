# syntax=docker/dockerfile:1
# 只构建 MyFlow 前端。构建上下文是仓库根：
#   docker build -f docker/frontend.Dockerfile -t myflow-frontend:local .
#
# Next standalone 监听 127.0.0.1:3000，nginx 对外监听 2026，并把 /api 转到
# GATEWAY_UPSTREAM（默认宿主机 8001）。config.yaml 与 .env 打进 /app，
# 前端进程不读取它们。

FROM node:22-alpine AS deps
WORKDIR /app

RUN corepack enable && corepack prepare pnpm@11.22.0 --activate

COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN sed -i 's#https://registry.npmjs.org/#https://registry.npmmirror.com/#g' pnpm-lock.yaml \
    && pnpm config set fetch-retries 5 \
    && pnpm config set fetch-timeout 300000 \
    && pnpm install --frozen-lockfile

FROM node:22-alpine AS builder
WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_API_BASE_URL=/api \
    NEXT_PUBLIC_MAX_UPLOAD_BYTES=26214400 \
    NEXT_PUBLIC_ARTIFACT_PREVIEW_BYTES=1048576

COPY --from=deps /app/node_modules ./node_modules
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
COPY frontend/next.config.ts frontend/tsconfig.json frontend/postcss.config.mjs ./
COPY frontend/src ./src

RUN mkdir -p public && ./node_modules/.bin/next build

FROM node:22-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    GATEWAY_UPSTREAM=host.docker.internal:8001

RUN apk add --no-cache nginx \
    && addgroup -g 1001 -S nodejs \
    && adduser -u 1001 -S nextjs -G nodejs \
    && mkdir -p /tmp/nginx/client-body /tmp/nginx/proxy /tmp/nginx/fastcgi \
                /tmp/nginx/uwsgi /tmp/nginx/scgi \
    && chown -R nextjs:nodejs /tmp/nginx /app

COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
COPY --chown=root:root frontend/docker/nginx.conf /etc/nginx/nginx.conf.template
COPY --chown=nextjs:nodejs config.yaml /app/config.yaml
COPY --chown=nextjs:nodejs .env /app/.env

USER nextjs

EXPOSE 2026

CMD ["sh", "-c", "HOSTNAME=0.0.0.0 PORT=3000 node server.js & for i in $(seq 1 50); do wget -q -O /dev/null http://127.0.0.1:3000/ && break; sleep 0.2; done; case \"$GATEWAY_UPSTREAM\" in *[!A-Za-z0-9.:_-]*) echo \"invalid GATEWAY_UPSTREAM\" >&2; exit 1;; esac; sed \"s#__GATEWAY_UPSTREAM__#$GATEWAY_UPSTREAM#g\" /etc/nginx/nginx.conf.template > /tmp/nginx.conf && exec nginx -c /tmp/nginx.conf -g 'daemon off;'"]
