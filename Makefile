# Code Interpreter Sandbox — developer & ops task runner.
# Run `make help` for the full list of targets.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ── Configuration ────────────────────────────────────────────────────────────────────────────
REGISTRY      ?= ghcr.io/your-org
TAG           ?= latest
LANGS         := python javascript typescript java go ruby rust bash
COMPOSE       := docker compose
COMPOSE_PROD  := docker compose -f docker-compose.yml -f docker-compose.prod.yml
PYTHON        ?= python3
PYTEST        := $(PYTHON) -m pytest
# Trivy severities that fail a scan.
TRIVY_SEVERITY ?= HIGH,CRITICAL

.PHONY: help
help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ── Development ───────────────────────────────────────────────────────────────────────────────
.PHONY: dev
dev: build-runtimes ## Start the full local stack (hot reload via compose build).
	$(COMPOSE) up --build

.PHONY: dev-detached
dev-detached: build-runtimes ## Start the stack in the background.
	$(COMPOSE) up --build -d

.PHONY: down
down: ## Stop the stack (keep volumes).
	$(COMPOSE) down

# ── Build ─────────────────────────────────────────────────────────────────────────────────────
.PHONY: build
build: build-runtimes build-services ## Build all images (runtimes + api + worker).

.PHONY: build-runtimes
build-runtimes: ## Build the 8 language runtime images.
	@for lang in $(LANGS); do \
	  echo "==> building sandbox-runtime-$$lang:$(TAG)"; \
	  DOCKER_BUILDKIT=1 docker build -t sandbox-runtime-$$lang:$(TAG) runtimes/$$lang || exit 1; \
	done

.PHONY: build-services
build-services: ## Build the API and worker images.
	DOCKER_BUILDKIT=1 docker build -t code-sandbox-api:$(TAG) api
	DOCKER_BUILDKIT=1 docker build -t code-sandbox-worker:$(TAG) worker

# ── Tests ─────────────────────────────────────────────────────────────────────────────────────
.PHONY: test
test: test-unit test-integration test-e2e ## Run the full test suite.

.PHONY: test-unit
test-unit: ## Unit tests (Python + TypeScript), no external services required.
	$(PYTEST) tests/unit -m "not integration" -v
	cd api && npm run test

.PHONY: test-integration
test-integration: ## Integration tests against real Redis + Docker.
	$(PYTEST) tests/integration tests/security -m integration -v

.PHONY: test-e2e
test-e2e: ## End-to-end tests against the running compose stack.
	cd api && npx vitest run --config ../tests/e2e/vitest.e2e.config.ts

.PHONY: test-coverage
test-coverage: ## Unit tests with coverage gates (≥90% line / ≥85% branch).
	$(PYTEST) tests/unit -m "not integration" --cov=worker.sandbox \
	  --cov-report=term-missing --cov-report=html --cov-report=lcov \
	  --cov-fail-under=90
	cd api && npm run test:coverage

# ── Lint / type-check ─────────────────────────────────────────────────────────────────────────
.PHONY: lint
lint: lint-py lint-ts ## Lint and type-check all code.

.PHONY: lint-py
lint-py: ## Ruff + mypy on the worker.
	$(PYTHON) -m ruff check worker tests
	$(PYTHON) -m ruff format --check worker tests
	$(PYTHON) -m mypy worker

.PHONY: lint-ts
lint-ts: ## ESLint + tsc on the API.
	cd api && npm run lint && npm run typecheck

# ── Security ──────────────────────────────────────────────────────────────────────────────────
.PHONY: security-scan
security-scan: ## Trivy-scan all built images (fails on HIGH/CRITICAL).
	@for img in $(addprefix sandbox-runtime-,$(LANGS)) code-sandbox-api code-sandbox-worker; do \
	  echo "==> trivy $$img:$(TAG)"; \
	  trivy image --severity $(TRIVY_SEVERITY) --exit-code 1 --no-progress $$img:$(TAG) || exit 1; \
	done

.PHONY: seccomp-regen
seccomp-regen: ## Regenerate the static seccomp profiles from the builder.
	@for lang in $(LANGS); do \
	  PYTHONPATH=. $(PYTHON) -m worker.sandbox.seccomp $$lang > runtimes/$$lang/seccomp-profile.json; \
	  echo "regenerated runtimes/$$lang/seccomp-profile.json"; \
	done

# ── Release ───────────────────────────────────────────────────────────────────────────────────
.PHONY: push-runtimes
push-runtimes: build-runtimes ## Tag and push runtime images to $(REGISTRY).
	@for lang in $(LANGS); do \
	  docker tag sandbox-runtime-$$lang:$(TAG) $(REGISTRY)/sandbox-runtime-$$lang:$(TAG); \
	  docker push $(REGISTRY)/sandbox-runtime-$$lang:$(TAG); \
	done

.PHONY: push-services
push-services: build-services ## Tag and push the API + worker images.
	docker tag code-sandbox-api:$(TAG) $(REGISTRY)/code-sandbox-api:$(TAG)
	docker tag code-sandbox-worker:$(TAG) $(REGISTRY)/code-sandbox-worker:$(TAG)
	docker push $(REGISTRY)/code-sandbox-api:$(TAG)
	docker push $(REGISTRY)/code-sandbox-worker:$(TAG)

# ── Load testing ──────────────────────────────────────────────────────────────────────────────
.PHONY: load-test
load-test: ## Run the k6 load-test scenario against the local API.
	k6 run tests/e2e/load_test.js

# ── Cleanup ───────────────────────────────────────────────────────────────────────────────────
.PHONY: clean
clean: ## Remove containers, volumes, caches, and temp files.
	-$(COMPOSE) down -v --remove-orphans
	-docker ps -aq --filter "label=sandbox-managed=1" | xargs -r docker rm -f
	-rm -rf api/dist api/coverage worker/__pycache__ .pytest_cache htmlcov
	-rm -rf /tmp/code-sandbox-work /tmp/sandbox-seccomp
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

.PHONY: install
install: ## Install all dev dependencies (node + python).
	cd api && npm install
	$(PYTHON) -m pip install -r worker/requirements.txt -r tests/requirements.txt
