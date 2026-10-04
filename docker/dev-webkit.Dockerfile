# Development-only Playwright WebKit server for hosts whose libraries cannot run
# Playwright's Ubuntu WebKit build (for example Fedora). Never part of a deployment.
FROM docker.io/library/node:24.20.0-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS node

FROM mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
WORKDIR /opt/aegis-webkit
COPY frontend/package.json frontend/package-lock.json ./
RUN /usr/local/bin/node /usr/local/lib/node_modules/npm/bin/npm-cli.js ci \
        --ignore-scripts --no-audit --no-fund \
    && /usr/local/bin/node -e 'const v = JSON.parse(require("fs").readFileSync("node_modules/@playwright/test/package.json", "utf8")).version; if (v !== "1.63.0") { console.error(v); process.exit(1); }'
USER pwuser
