.PHONY: test lint typecheck check verify verify-compose test-e2e benchmark-catalog

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

# Opt-in, hours-scale: the full 1M-entry/50K-folder catalog benchmark in its own
# owned stack. BENCHMARK_REPORT_DIR must be a fresh private directory, for example
# BENCHMARK_REPORT_DIR=$$(mktemp -d /tmp/aegis-phase2a-reports.XXXXXXXX).
benchmark-catalog:
	@test -n "$(BENCHMARK_REPORT_DIR)" || { echo "BENCHMARK_REPORT_DIR is required" >&2; exit 64; }
	uv run --locked python -m scripts.benchmarks.run catalog --entries 1000000 --wide-folder 50000 --report-dir "$(BENCHMARK_REPORT_DIR)"
