FROM postgres:18.6-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2

COPY --chmod=0555 deploy/postgres/entrypoint.sh /usr/local/bin/aegis-postgres-entrypoint
COPY --chmod=0555 deploy/postgres/init/ /docker-entrypoint-initdb.d/

ENTRYPOINT ["/usr/local/bin/aegis-postgres-entrypoint"]
CMD ["postgres"]
