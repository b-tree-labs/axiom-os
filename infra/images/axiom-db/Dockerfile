# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# The database image a node runs: PostgreSQL 16 with TimescaleDB, its toolkit
# and pgvector, from the Debian packages.
#
# Every TimescaleDB package is pinned, the loader included. The loader owns
# timescaledb.control; left unpinned, apt satisfies the engine's
# "loader >= engine" dependency with the newest loader, whose control file
# names a default version this image has no install script for. An existing
# database never notices; CREATE EXTENSION on a new one fails, and so does a
# restore (#1598). The last step proves it cannot happen here: it creates every
# required extension in an empty database and fails the build otherwise.
#
# Debian, not Alpine: a data directory keeps the C library it was initialised
# under (text collation lives in the indexes), so a node moves between the two
# only by dump and restore.
FROM postgres:16.16-bookworm

ARG TIMESCALEDB_VERSION=2.28.2
ARG TOOLKIT_VERSION=1.24.0
# PGDG keeps only its newest build of a package, so pgvector is pinned to its
# minor release; the check below proves whatever it installed can be created.
ARG PGVECTOR_VERSION=0.8

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/timescaledb.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/timescaledb.gpg] https://packagecloud.io/timescale/timescaledb/debian/ bookworm main" \
    > /etc/apt/sources.list.d/timescaledb.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends timescaledb-2-postgresql-16=${TIMESCALEDB_VERSION}~* timescaledb-2-loader-postgresql-16=${TIMESCALEDB_VERSION}~* timescaledb-toolkit-postgresql-16=1:${TOOLKIT_VERSION}~* postgresql-16-pgvector=${PGVECTOR_VERSION}.* \
 && apt-mark hold timescaledb-2-postgresql-16 timescaledb-2-loader-postgresql-16 timescaledb-toolkit-postgresql-16 \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

# A new cluster preloads the engine; an existing data directory keeps its own
# postgresql.conf, which already does.
RUN sed -ri "s/^#?(shared_preload_libraries)\s*=\s*'[^']*'/\1 = 'timescaledb'/" \
    /usr/share/postgresql/postgresql.conf.sample \
 && grep -q "^shared_preload_libraries = 'timescaledb'" /usr/share/postgresql/postgresql.conf.sample

COPY check-extensions.sh /usr/local/bin/axiom-check-extensions
RUN chmod 0755 /usr/local/bin/axiom-check-extensions \
 && su postgres -c "/usr/local/bin/axiom-check-extensions"
