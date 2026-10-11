# The aegis-media image (M2): the backend runtime plus Debian's ffmpeg, running only the media
# processor. Same pinned bases as docker/backend.Dockerfile (deploy/images.lock).
#
# ffmpeg is not version-pinned: debian-security keeps only the latest trixie version, so a pin
# would break every build after a security update. The final stage records what it installed
# (ffmpeg, ffprobe, Pillow, pillow-heif, libheif) in /usr/share/aegis/media-tools.json.

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

# Never published: the same environment plus the locked dev group, so the ffmpeg-marked
# processor tests can run inside this image (tests/deployment/test_media_image.py).
FROM build AS test-environment
RUN uv sync --locked --group dev --no-editable

FROM docker.io/library/python:3.13.15-slim-trixie@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2 AS runtime

ARG AEGIS_UID=10001
ARG AEGIS_GID=10001
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 aegis \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin aegis \
    && install -d -o "$AEGIS_UID" -g "$AEGIS_GID" -m 0770 /srv/aegis/scratch
WORKDIR /app/backend

FROM runtime AS media-test
COPY --from=test-environment /app/.venv /app/.venv
COPY --from=test-environment /app/backend /app/backend
USER 10001:10001

FROM runtime

LABEL org.aegis.ffmpeg.version="see /usr/share/aegis/media-tools.json"
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/backend /app/backend
RUN install -d -m 0755 /usr/share/aegis \
    && python -I -B -m aegis_media_processor.inventory /usr/share/aegis/media-tools.json
USER 10001:10001

ENTRYPOINT ["python", "-m", "aegis_media_processor"]
CMD []
