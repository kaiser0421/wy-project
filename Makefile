SHELL := /bin/bash
COMPOSE := docker compose
TEST_COMPOSE := docker compose -p ticketing-test -f compose.test.yaml

.PHONY: help up down logs ps test test-fast lint fmt typecheck smoke verify-all clean

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

up:  ## Build and start every service (same as: docker compose up -d --build)
	$(COMPOSE) up -d --build

down:  ## Stop services (keeps volumes)
	$(COMPOSE) down

logs:  ## Follow service logs
	$(COMPOSE) logs -f --tail=100

ps:  ## Show service status
	$(COMPOSE) ps

test:  ## Run backend checks, PostgreSQL integration tests, and frontend tests in Docker
	@trap '$(TEST_COMPOSE) down -v --remove-orphans' EXIT; \
		status=0; \
		$(TEST_COMPOSE) build tests && $(TEST_COMPOSE) run --rm -T tests || status=$$?; \
		$(TEST_COMPOSE) run --rm -T frontend-tests || status=$$?; \
		exit $$status

test-fast:  ## Unit tests only, on the host (no Docker)
	uv run pytest backend/tests/unit -q

lint:  ## Lint Python, import contracts, shell scripts and frontend TypeScript
	uv run ruff check backend paymock scripts loadtest
	uv run ruff format --check backend paymock scripts loadtest
	uv run lint-imports
	docker run --rm -v "$(CURDIR):/mnt" -w /mnt koalaman/shellcheck:v0.10.0 -S warning \
		loadtest/run.sh scripts/*.sh
	cd frontend && npm run lint

fmt:  ## Auto-format
	uv run ruff format backend paymock scripts loadtest
	uv run ruff check --fix backend paymock scripts loadtest

typecheck:  ## mypy --strict
	uv run mypy backend/src paymock/src

smoke:  ## Start the stack and verify migrate/seed exit 0, 3 healthy API replicas, ingress working
	./scripts/smoke.sh

verify-all: test smoke  ## Full verification (extended in later milestones)

clean:  ## Stop services and delete volumes
	$(COMPOSE) down -v --remove-orphans
