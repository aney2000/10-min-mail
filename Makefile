# Developer entry points.
#
# Why a Makefile on a Python project? Because "how do I check my work?"
# should have exactly one answer, and that answer should be short enough
# that nobody skips it. `make check` is the gate every commit must pass.
#
# On Windows, run these through `make` from Git Bash / WSL, or invoke the
# underlying commands directly -- they are plain one-liners on purpose.

PY := python

.PHONY: help install lint format typecheck test check

help:
	@echo "install    - install the package with dev dependencies"
	@echo "lint       - ruff check (report only)"
	@echo "format     - ruff format + ruff check --fix (rewrites files)"
	@echo "typecheck  - mypy --strict"
	@echo "test       - pytest"
	@echo "check      - lint + typecheck + test  (the commit gate)"

install:
	$(PY) -m pip install -e ".[dev]"

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff format .
	$(PY) -m ruff check . --fix

typecheck:
	$(PY) -m mypy

test:
	$(PY) -m pytest

# Coverage is a regression alarm, not a goal -- see the note in
# pyproject.toml. The threshold lives there so `pytest --cov` enforces
# it identically here, in CI, and on a developer's machine.
coverage:
	$(PY) -m pytest --cov

# The single gate. Runs in dependency order: cheap/fast checks first so
# failures surface quickly, tests last because they are the slowest.
check: lint typecheck coverage
	@echo ""
	@echo "All checks passed."

# Run the containerised stack. Requires Docker.
docker-up:
	docker compose up --build

docker-down:
	docker compose down
