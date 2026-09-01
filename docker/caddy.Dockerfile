FROM caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648

USER root
RUN setcap -r /usr/bin/caddy \
    && install -d -o 10001 -g 10001 -m 0700 /data /config \
    && test -z "$(getcap /usr/bin/caddy)"
COPY --chmod=0755 deploy/caddy/aegis-caddy-start /usr/local/bin/aegis-caddy-start
USER 10001:10001
