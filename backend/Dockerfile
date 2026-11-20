# syntax=docker/dockerfile:1.7
# Multi-stage image for the backend (api, worker, relay, collector, migrate, seed).
#   runtime : production image, no dev dependencies, no tests, non-root, read-only friendly
#   dev     : runtime + dev dependencies + tests (used by compose.test.yaml only)

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
    uv sync --frozen --no-dev --no-install-workspace --package ticketing
COPY backend/src backend/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package ticketing --no-editable

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app \
 && mkdir -p /data/logs /data/imports /data/exports && chown -R app:app /data
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY backend/alembic.ini ./alembic.ini
COPY backend/migrations ./migrations
USER app
EXPOSE 8000
CMD ["ticketing-api"]

FROM builder AS dev-builder
COPY backend/tests backend/tests
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package ticketing --group dev

FROM python:${PYTHON_VERSION}-slim AS dev
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=dev-builder /opt/venv /opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY backend backend
CMD ["pytest"]
