FROM ghcr.io/astral-sh/uv:0.12.8@sha256:d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a AS uv
FROM python:3.13.15-slim-trixie@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2 AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock ./
COPY backend/manage.py ./backend/manage.py
COPY backend/aegis ./backend/aegis
COPY backend/aegis_apps ./backend/aegis_apps
COPY backend/aegisctl ./backend/aegisctl
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13.15-slim-trixie@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10001 aegis \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin aegis
WORKDIR /app/backend
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/backend /app/backend
USER 10001:10001

CMD ["uvicorn", "aegis.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--log-config", "/app/backend/aegis/uvicorn_logging.json"]
