FROM ghcr.io/astral-sh/uv:0.12.8@sha256:d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a AS uv
FROM docker.io/library/python:3.13.15-slim-trixie@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2 AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock ./
COPY backend/manage.py ./backend/manage.py
COPY backend/aegis ./backend/aegis
COPY backend/aegis_apps ./backend/aegis_apps
COPY backend/aegisctl ./backend/aegisctl
COPY backend/aegis_media_processor ./backend/aegis_media_processor
RUN uv sync --locked --no-dev --no-editable
# Processes import the app from /app/backend (manage.py's directory, the scan reader's
# PYTHONPATH), not from the venv. Compile it here: a read-only container with
# PYTHONDONTWRITEBYTECODE reads these files but would otherwise recompile the source in every
# reader child and health check. Hash-checked files stay valid whatever mtimes layers keep.
RUN python -m compileall -q -j 0 --invalidation-mode checked-hash \
    backend/aegis backend/aegis_apps backend/aegisctl backend/aegis_media_processor

# The rootless Podman installer and its templates, for operators to copy out of the image.
# Root-owned and read-only; the final stage takes them in one layer with these modes.
FROM docker.io/library/python:3.13.15-slim-trixie@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2 AS podman-files

COPY deploy/podman /usr/share/aegis/podman
RUN find /usr/share/aegis/podman -type f -exec chmod 0444 {} + \
    && find /usr/share/aegis/podman -type d -exec chmod 0555 {} + \
    && chmod 0555 /usr/share/aegis/podman/aegis-podman \
    && test -z "$(find /usr/share/aegis/podman ! -type f ! -type d -o -name __pycache__ -o -name '*.pyc')"

FROM docker.io/library/python:3.13.15-slim-trixie@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
ARG AEGIS_UID=10001
ARG AEGIS_GID=10001
# The base image ships the standard library without bytecode, so every short-lived process
# (each scan reader child, each health check) would recompile the modules it imports.
RUN python -m compileall -q -j 0 --invalidation-mode checked-hash -x /site-packages/ \
    /usr/local/lib/python3.13
# Volume mount points, owned by the runtime user: a fresh named volume takes its owner and
# mode from the image's directory, so the media worker can write it from the first start.
# Scratch is group-writable: the processor (same identity) claims and publishes jobs there.
RUN groupadd --gid 10001 aegis \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin aegis \
    && install -d -o "$AEGIS_UID" -g "$AEGIS_GID" -m 0700 /srv/aegis/indexer-coordination \
    && install -d -o "$AEGIS_UID" -g "$AEGIS_GID" -m 0750 /srv/aegis/derivatives /srv/aegis/quarantine \
    && install -d -o "$AEGIS_UID" -g "$AEGIS_GID" -m 0770 /srv/aegis/scratch
WORKDIR /app/backend
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/backend /app/backend
COPY --from=podman-files /usr/share/aegis /usr/share/aegis
USER 10001:10001

CMD ["uvicorn", "aegis.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--log-config", "/app/backend/aegis/uvicorn_logging.json"]
