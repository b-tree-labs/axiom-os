# Axiom — Development Makefile
#
# Setup:
#   make install       Install axiom in editable mode + hooks
#
# Development:
#   make check         Run all local gates (lint + test) — mirrors CI
#   make test          Unit tests only
#   make lint          Ruff lint only

.PHONY: install check test test-all lint build clean help dev-fault dev dev-status dev-down dev-logs dev-check dev-shot dev-db

# ─── Run it locally (docs/conventions/local-run.md) ──────────────────────────

# This checkout's own source, never whatever `axi` is on PATH (an installed
# release lacks these verbs). The venv python is the shared one beside the
# worktrees, or this checkout's own, else python3.
PYTHON ?= $(firstword $(wildcard $(CURDIR)/.venv/bin/python $(CURDIR)/../.venv/bin/python) python3)
AXI ?= env PYTHONPATH=$(CURDIR)/src $(PYTHON) -m axiom.axiom_cli

# TOPOLOGY=<name> runs every node of a topology, each on its own lane (ADR-255):
# solo, team, collect, relay, multi-tenant, or one declared in dev.toml.
DEV_TOPOLOGY = $(if $(TOPOLOGY),--topology $(TOPOLOGY),)

dev:  ## Start this checkout live (hot reload), print the one URL (TOPOLOGY=<name> for several nodes)
	@$(AXI) dev up $(DEV_TOPOLOGY)

dev-status:  ## Whether it is up, and its URL (exit 1 when down)
	@$(AXI) dev status

dev-down:  ## Stop only what `make dev` started
	@$(AXI) dev down

dev-logs:  ## Print the node's and the web server's log paths, and tail them
	@$(AXI) dev status --json | python3 -c "import json,sys,pathlib; v=json.load(sys.stdin); logs=[v.get('log'), str(pathlib.Path(v['log']).with_name('web.log')) if v.get('log') else None]; logs=[l for l in logs if l and pathlib.Path(l).exists()]; print('\\n'.join(logs)); sys.exit(0 if logs else 1)" | tee /dev/stderr | xargs tail -n 40

dev-fault:  ## Inject a fault at an edge: make dev-fault NODE=probe FAULT=home-down|source-offline|slow-link|clock-skew|clear
	@$(AXI) dev fault $(NODE) $(FAULT) $(VALUE)

dev-check:  ## Smoke test: hot-reload loop served within 3 s, then stop (TOPOLOGY=<name>: all readings arrive once)
	@$(AXI) dev check $(DEV_TOPOLOGY)

dev-shot:  ## Write screenshots to .dev/shots/ (the checkout's [dev].shots command)
	@$(AXI) dev shot

dev-db:  ## This worktree's schema: make dev-db CMD=status|migrate|reset|seed (reset needs YES=1)
	@$(AXI) dev db $(or $(CMD),status) $(if $(YES),--yes,)

# ─── Setup ───────────────────────────────────────────────────────────────────

install:  ## Install axiom in editable mode with all extras + hooks
	pip install -e ".[all]" && pip install ruff pre-commit
	pre-commit install
	@$(MAKE) --no-print-directory hooks

hooks:  ## Install/refresh the repo git hooks (idempotent — safe to re-run)
	@HOOKS="$$(git rev-parse --git-common-dir)/hooks"; \
	mkdir -p "$$HOOKS"; \
	for h in pre-push commit-msg; do \
	  if [ -f "scripts/hooks/$$h" ]; then \
	    cp "scripts/hooks/$$h" "$$HOOKS/$$h" && chmod +x "$$HOOKS/$$h" \
	      && echo "  installed $$h"; \
	  fi; \
	done; \
	echo "hooks installed to $$HOOKS"

# ─── Quality Gates (ordered fast → slow) ────────────────────────────────────

check: lint test  ## Run all local gates — equivalent to CI test stage

lint:  ## Run ruff linter
	ruff check src/ --select E,F,W --ignore E501

test:  ## Run unit tests (no credentials needed)
	pytest tests/ src/axiom/extensions/builtins/ -n auto -v --tb=short -m "not integration"

test-all:  ## Run all tests (unit + integration)
	pytest tests/ src/axiom/extensions/builtins/ -n auto -v --tb=short

# ─── Build & Distribution ────────────────────────────────────────────────────

build:  ## Build wheel and sdist
	pip install build && python -m build

clean:  ## Remove build artifacts
	rm -rf dist/ build/ *.egg-info .pytest_cache .pip-cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ─── Help ────────────────────────────────────────────────────────────────────

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
