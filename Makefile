# ShowWatch developer commands (SPEC §6). Everything runs in the uv-managed environment;
# --locked fails fast if pyproject.toml and uv.lock have drifted apart.
UV  ?= uv
RUN := $(UV) run --locked

.DEFAULT_GOAL := help
.PHONY: help sync check test e2e fakesite fmt

help: ## List the available targets
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  make %-9s %s\n", $$1, $$2}'

sync: ## Create or refresh .venv from uv.lock
	$(UV) sync --locked

check: ## Lint, format check, type check and the full test suite (must be green)
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) mypy --strict src scripts
	$(RUN) mypy tests
	$(RUN) pytest -q

test: ## Run the test suite
	$(RUN) pytest

e2e: ## Run only the end-to-end (Playwright) tests
	$(RUN) pytest -m e2e

fakesite: ## Run the local fake site for manual poking (Phase 6+)
	@test -f tests/fakesite/__main__.py || { echo "make fakesite: the fake site arrives in Phase 6." >&2; exit 1; }
	$(RUN) python -m tests.fakesite

fmt: ## Auto-format and apply safe lint fixes
	$(RUN) ruff format .
	$(RUN) ruff check --fix .
