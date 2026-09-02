FROM node:24.20.0-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.12.8@sha256:d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a AS admin-static-uv
FROM python:3.13.15-slim-trixie@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2 AS admin-static-build

ENV PATH="/app/.venv/bin:$PATH" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY --from=admin-static-uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock ./
COPY backend/manage.py ./backend/manage.py
COPY backend/aegis ./backend/aegis
COPY backend/aegis_apps ./backend/aegis_apps
RUN uv sync --locked --no-dev --no-editable
WORKDIR /app/backend
RUN python manage.py collectstatic --noinput

FROM nginxinc/nginx-unprivileged:1.30.4-alpine@sha256:45ce1e2e699234253d1def7baa96218a5d00b498d1ba0cbb1a17b6bdf73d1351

ENV NGINX_ENTRYPOINT_QUIET_LOGS=1
COPY deploy/nginx/nginx.conf /etc/nginx/nginx.conf
COPY deploy/nginx/aegis-server.conf /etc/nginx/aegis-server.conf
COPY --chmod=0555 deploy/nginx/start-gateway.sh /usr/local/bin/aegis-gateway-start
COPY --from=frontend-build /frontend/dist/ /usr/share/nginx/html/
COPY --from=admin-static-build /app/backend/staticfiles/admin/ /usr/share/nginx/html/admin-static/admin/
EXPOSE 8080
USER 101:101
CMD ["/usr/local/bin/aegis-gateway-start"]
