#!/bin/sh
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Prove every extension the platform needs can be created on an empty database.
# Runs at image build (as postgres) against a throwaway cluster, so an image
# whose control file names a version it cannot install is never tagged
# (#1598). Also safe to run in a live container: it never touches PGDATA.
set -eu

REQUIRED="timescaledb timescaledb_toolkit vector"
BIN=/usr/lib/postgresql/16/bin
WORK=$(mktemp -d)
trap '"$BIN/pg_ctl" -D "$WORK/data" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

"$BIN/initdb" -D "$WORK/data" -U postgres --auth=trust >/dev/null
"$BIN/pg_ctl" -D "$WORK/data" -w -l "$WORK/log" \
  -o "-c listen_addresses='' -k $WORK -c shared_preload_libraries=timescaledb" start >/dev/null

psql() { "$BIN/psql" -h "$WORK" -U postgres -d postgres -v ON_ERROR_STOP=1 -qAt "$@"; }

for ext in $REQUIRED; do
  # The control file's default must have an install script, or CREATE fails.
  missing=$(psql -c "select e.name || ' ' || e.default_version from pg_available_extensions e
                     where e.name = '$ext' and not exists (
                       select 1 from pg_available_extension_versions v
                       where v.name = e.name and v.version = e.default_version)")
  if [ -n "$missing" ]; then
    echo "check-extensions: default version has no install script: $missing" >&2
    exit 1
  fi
  psql -c "CREATE EXTENSION IF NOT EXISTS $ext CASCADE"
  echo "check-extensions: $ext $(psql -c "select extversion from pg_extension where extname = '$ext'")"
done
