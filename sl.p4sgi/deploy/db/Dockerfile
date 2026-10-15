# postgres:16-alpine + pgvector, built on the SAME base image the data directory was created with.
# (Do not switch to a Debian-based pgvector image: glibc vs musl collation differences can corrupt text indexes.)
FROM postgres:16-alpine
ARG PGVECTOR_VERSION=v0.8.0
RUN apk add --no-cache --virtual .build-deps git build-base \
 && git clone --branch ${PGVECTOR_VERSION} --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector \
 && cd /tmp/pgvector && make with_llvm=no && make with_llvm=no install \
 && cd / && rm -rf /tmp/pgvector && apk del .build-deps
