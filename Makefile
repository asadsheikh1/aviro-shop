# AVIRO developer commands. `make help` lists them.
SHELL := /bin/bash
.DEFAULT_GOAL := help
COMPOSE := podman compose

.PHONY: help install lint test up up-otel up-sentinel down reset ps logs otel-logs load load-stop smoke deploy fault

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies
	uv sync --locked

lint: ## ruff + mypy
	uv run ruff check . && uv run ruff format --check . && uv run mypy

test: ## Unit tests (no containers)
	uv run pytest

up: ## Build and start the shop on http://127.0.0.1:8080 (data volume kept)
	$(COMPOSE) build
	@# podman-compose recreates only on config change, not image change: force it.
	$(COMPOSE) up -d --force-recreate

up-otel: ## Same as up, plus a debug OTel collector receiving traces/metrics/logs
	$(COMPOSE) build
	OTEL_EXPORTER=otlp $(COMPOSE) --profile otel up -d --force-recreate

up-sentinel: ## Send telemetry to SENTINEL: make up-sentinel TOKEN=sit_... (SENTINEL must be running)
	@test -n "$(TOKEN)" || (echo 'usage: make up-sentinel TOKEN=sit_...'; exit 1)
	$(COMPOSE) build
	OTEL_EXPORTER=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://ingest.sentinel.internal:4319 \
	OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20$(TOKEN)" \
	$(COMPOSE) -f compose.yaml -f compose.sentinel.yaml up -d --force-recreate

down: ## Stop everything (data kept)
	$(COMPOSE) -f compose.yaml -f compose.sentinel.yaml --profile otel --profile load down

reset: ## DELETE the database volume and start fresh
	$(COMPOSE) --profile otel --profile load down -v
	$(MAKE) up

ps: ## Container status
	$(COMPOSE) ps

logs: ## Follow service logs
	$(COMPOSE) logs -f gateway inventory payments orders worker

otel-logs: ## What the debug collector received
	$(COMPOSE) --profile otel logs -f otel-debug

load: ## Start synthetic shoppers: make load USERS=20 DURATION=15m
	LOAD_USERS=$${USERS:-10} LOAD_DURATION=$${DURATION:-10m} $(COMPOSE) --profile load up -d loadgen

load-stop: ## Stop the load generator
	$(COMPOSE) --profile load stop loadgen

smoke: ## Verify every fault reproduces against the running stack (make up first)
	uv run pytest -m smoke -v

deploy: ## Deploy a git ref: make deploy REF=scenario/S01-bad
	scripts/deploy.sh $(REF)

fault: ## Fault CLI: make fault ARGS="set payments latency ms=8000"
	uv run python scripts/fault.py $(ARGS)
