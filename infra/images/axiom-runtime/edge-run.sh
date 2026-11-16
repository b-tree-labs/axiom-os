#!/bin/sh
# The ingest edge (ADR-177). Who may push is read from /config, which the
# operator owns, so adding or removing a partner needs no restart:
#   /config/sources.txt         "<source>=<site>" per line (# comments ok)
#   /config/gate-api-keys.json  issued keys, hashes only
set -eu
PORT="${1:-8787}"
export AXI_STATE_DIR="${AXI_STATE_DIR:-/data/state}"
export AXIOM_INGEST_OUTBOX_DIR="${AXIOM_INGEST_OUTBOX_DIR:-/data/outbox}"
export AXIOM_GATE_API_KEYS_FILE="${AXIOM_GATE_API_KEYS_FILE:-/config/gate-api-keys.json}"
export AXIOM_ACTOR="${AXIOM_ACTOR:-@ingest-edge:edge}"
SOURCES_FILE="${EDGE_SOURCES_FILE:-/config/sources.txt}"
mkdir -p "$AXI_STATE_DIR" "$AXIOM_INGEST_OUTBOX_DIR" /data/bronze
reconcile() {
  [ -r "$SOURCES_FILE" ] || { echo "edge: no $SOURCES_FILE yet; no partner can push" >&2; return 0; }
  known="$(axi data list 2>/dev/null || true)"
  grep -vE '^[[:space:]]*(#|$)' "$SOURCES_FILE" | while IFS= read -r pair; do
    pair="$(echo "$pair" | tr -d '[:space:]')"; source="${pair%%=*}"; site="${pair#*=}"
    if [ -z "$source" ] || [ -z "$site" ] || [ "$source" = "$pair" ]; then
      echo "edge: skipping '$pair' (want <source>=<site>)" >&2; continue
    fi
    echo "$known" | grep -q "name=${source} " && continue
    if axi data register --bronze-root "/data/bronze/${source}" --site "$site" \
         --default-disposition allow --default-tier restricted "$source" push >/dev/null 2>&1; then
      echo "edge: now accepting ${source} for site ${site}"
    else
      echo "edge: could not register ${source} for ${site}" >&2
    fi
  done
}
[ -s "$AXIOM_GATE_API_KEYS_FILE" ] || echo "edge: no keys file at $AXIOM_GATE_API_KEYS_FILE; every push will be refused" >&2
reconcile
( while sleep "${EDGE_RECONCILE_SECONDS:-60}"; do reconcile; done ) &
exec axi serve --profile ingest-edge --host 0.0.0.0 --port "$PORT"
