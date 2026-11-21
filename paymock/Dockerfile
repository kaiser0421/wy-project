# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/pyproject.toml
COPY paymock/pyproject.toml paymock/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package paymock
COPY paymock/src paymock/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package paymock --no-editable

FROM python:${PYTHON_VERSION}-slim AS runtime
# Apply Debian security updates published after the base image was built.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app \
 && mkdir -p /data/paymock /data/logs && chown app:app /data/paymock /data/logs
COPY --from=builder /opt/venv /opt/venv
USER app
EXPOSE 8000
CMD ["paymock"]
