.PHONY: test lint typecheck check

test:
	uv run pytest

lint:
	uv run ruff check backend

typecheck:
	uv run mypy backend

check: test lint typecheck
