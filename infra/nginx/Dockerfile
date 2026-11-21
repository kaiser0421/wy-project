# syntax=docker/dockerfile:1.7
# Ingress image: builds the web UI and serves it with the API reverse-proxy configuration.
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# 1.28 is the maintained stable branch (upstream `resolve` needs >= 1.27.3); apk upgrade
# pulls distro security fixes published after the base image was built.
FROM nginx:1.28-alpine
RUN apk upgrade --no-cache
COPY infra/nginx/nginx.conf /etc/nginx/nginx.conf
COPY --from=web /web/dist /usr/share/nginx/html
