.PHONY: test lint typecheck check verify verify-compose test-e2e

test:
	uv run --locked pytest

lint:
	uv run --locked ruff check backend

typecheck:
	uv run --locked mypy backend

check: test lint typecheck

verify:
	uv sync --locked --group dev
	uv lock --check
	uv run --locked ruff check backend tests scripts
	uv run --locked mypy backend
	uv run --locked python scripts/verify.py backend
	npm --prefix frontend ci
	npm --prefix frontend run lint
	npm --prefix frontend run typecheck
	npm --prefix frontend test
	npm --prefix frontend run build
	git diff --check

verify-compose:
	uv run --locked python scripts/verify.py compose
	uv run --locked python scripts/verify.py deployment

test-e2e:
	bash scripts/test-e2e.sh
