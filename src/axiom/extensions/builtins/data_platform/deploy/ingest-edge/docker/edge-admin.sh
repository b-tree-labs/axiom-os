#!/bin/sh
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# The edge operator's tool: add a partner site, issue and revoke keys, look at
# the edge. Run it as the user who owns the config folder (EDGE_CONFIG_DIR, in
# the environment or in .env beside this script). It uses docker if the edge
# image is built here, otherwise an `axi` on the PATH.
#
#   ./edge-admin.sh add-site <site> [<source>]   registers <source> (default
#                                                <site>-src) and issues the site's key
#   ./edge-admin.sh new-key <site>               a second key for a site (rotation: both work)
#   ./edge-admin.sh add-puller                   issues the key the platform node pulls with
#   ./edge-admin.sh revoke <key_id>              refuses that key from now on
#   ./edge-admin.sh keys                         every key: id, site, scopes, revoked
#   ./edge-admin.sh status                       health, what the edge holds
#
# A new key is written to ./issued-keys/<name>.key (mode 600), never printed.
# Hand the file to its owner directly and delete it. Only a hash stays here.
set -eu

cd "$(dirname "$0")"
# Where the operator's config folder is: EDGE_CONFIG_DIR in the environment, or in
# .env beside this script (the one compose reads).
CFG="${EDGE_CONFIG_DIR:-}"
[ -n "$CFG" ] || { [ -f .env ] && CFG="$(sed -n 's/^EDGE_CONFIG_DIR=//p' .env | tail -1)"; }
[ -n "$CFG" ] && [ -d "$CFG" ] || { echo "set EDGE_CONFIG_DIR (or run from the folder with .env): the operator's config folder" >&2; exit 2; }
KEYS="$CFG/gate-api-keys.json"
SRC="$CFG/sources.txt"

# Keys are made by `axi gate`. Where docker works, by the very image the edge
# runs, so the two always agree. Where it does not (an operator who is not in the
# docker group), by an `axi` on the PATH, installed with
# `pip install --user "axiom-os-lm==<the version in compose.yaml>"`.
IMAGE="$(sed -n 's/^ *image: *//p' compose.yaml 2>/dev/null | head -1)"
if [ -z "${EDGE_ADMIN_LOCAL:-}" ] && [ -n "$IMAGE" ] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
  gate() {
    docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -e AXI_STATE_DIR=/tmp/state \
      -e AXIOM_ACTOR="${AXIOM_ACTOR:-@edge-operator:org}" \
      -v "$CFG:/config" --entrypoint axi "$IMAGE" gate --json --keys-file /config/gate-api-keys.json "$@"
  }
else
  command -v axi >/dev/null 2>&1 || { echo "need docker (with the edge image built) or axi on the PATH" >&2; exit 2; }
  gate() {
    AXIOM_ACTOR="${AXIOM_ACTOR:-@edge-operator:org}" axi gate --json --keys-file "$KEYS" "$@"
  }
fi

readable() {
  # The edge container runs as another user. Both files hold no usable secret
  # (hashes and names), and unreadable means every push is refused.
  [ -f "$KEYS" ] && chmod a+r "$KEYS"
  [ -f "$SRC" ] && chmod a+r "$SRC"
  # A release before the status-change-time fix reloads on mtime only.
  [ -f "$KEYS" ] && touch "$KEYS"
  return 0
}

token_of() {
  python3 -c 'import json,sys; print(json.load(sys.stdin)["value"]["token"], end="")' 2>/dev/null \
    || sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' | tr -d '\n'
}

issue() { # <name> <principal> [site] <scope>...
  name="$1"; principal="$2"; site="$3"; shift 3
  mkdir -p issued-keys; chmod 700 issued-keys
  [ -s "$KEYS" ] || { echo '[]' > "$KEYS"; chmod 644 "$KEYS"; }
  out="issued-keys/$name.key"
  [ ! -e "$out" ] || { echo "$out already exists: hand it over and delete it first" >&2; exit 1; }
  args=""
  for s in "$@"; do args="$args --scope $s"; done
  # shellcheck disable=SC2086
  if [ -n "$site" ]; then
    gate issue api-key --principal "$principal" --site "$site" $args --name "$name" > "issued-keys/.$name.json" 2> "issued-keys/.$name.err" || failed=1
  else
    gate issue api-key --principal "$principal" $args --name "$name" > "issued-keys/.$name.json" 2> "issued-keys/.$name.err" || failed=1
  fi
  if [ "${failed:-}" = 1 ]; then
    echo "could not issue the key:" >&2; tail -5 "issued-keys/.$name.err" >&2
    rm -f "issued-keys/.$name.json" "issued-keys/.$name.err"; exit 1
  fi
  rm -f "issued-keys/.$name.err"
  ( umask 077; token_of < "issued-keys/.$name.json" > "$out" )
  rm -f "issued-keys/.$name.json"
  chmod 600 "$out"
  readable
  echo "key written to $PWD/$out (mode 600). Give it to its owner directly, then delete the file."
}

cmd="${1:-}"; [ $# -gt 0 ] && shift
case "$cmd" in
  add-site)
    site="${1:?usage: add-site <site> [<source>]}"; source="${2:-$site-src}"
    case "$site$source" in *[!A-Za-z0-9._-]*) echo "site and source are letters, digits, . _ - only" >&2; exit 2;; esac
    touch "$SRC"
    grep -qx "$source=$site" "$SRC" || echo "$source=$site" >> "$SRC"
    readable
    issue "$site-ingest" "@$site-daq:$site" "$site" data_platform:invoke data_platform:read
    # The edge re-reads sources.txt once a minute. A partner that pushes before it
    # has, is refused as sending to a source that is not theirs, and their sender
    # sets those batches aside. So say when it is safe to hand the key over.
    c="$(docker ps --filter name=ingest-edge --format '{{.Names}}' 2>/dev/null | head -1)"
    if [ -n "$c" ]; then
      echo "waiting for the edge to register $source (it re-reads $SRC every minute) ..."
      i=0
      while [ $i -lt 24 ]; do
        if docker exec "$c" axi data list 2>/dev/null | grep -q "name=$source "; then
          echo "the edge accepts $source for site $site: the key can go to the partner now."; exit 0
        fi
        i=$((i + 1)); sleep 5
      done
      echo "the edge has not registered $source yet: check 'docker logs $c', and do not hand the key over until it says 'now accepting $source'." >&2
      exit 1
    fi
    echo "give it a minute before the partner's first push: the edge re-reads $SRC once a minute (its log says 'now accepting $source')."
    ;;
  new-key)
    site="${1:?usage: new-key <site>}"
    case "$site" in *[!A-Za-z0-9._-]*) echo "site is letters, digits, . _ - only" >&2; exit 2;; esac
    issue "$site-ingest-$(date +%Y%m%d%H%M%S)" "@$site-daq:$site" "$site" data_platform:invoke data_platform:read
    ;;
  add-puller)
    down="${EDGE_DOWNSTREAM:-$(sed -n 's/^AXIOM_EDGE_DOWNSTREAM=//p' edge.env 2>/dev/null | tail -1)}"
    [ -n "$down" ] || { echo "AXIOM_EDGE_DOWNSTREAM is not set in edge.env (or give EDGE_DOWNSTREAM=@name:context)" >&2; exit 2; }
    issue "platform-pull" "$down" "" edge_export:read
    ;;
  revoke)
    gate revoke api-key "${1:?usage: revoke <key_id>}" > /dev/null
    readable
    echo "revoked: refused from the next request."
    ;;
  keys)
    gate list api-keys | python3 -c '
import json,sys
for k in json.load(sys.stdin)["value"]["items"]:
    print("%-14s %-24s %-10s %-34s %s" % (k["key_id"], k["principal"], k.get("site") or "-", ",".join(k["scopes"]), "REVOKED" if k.get("revoked_at") else "active"))' \
    || gate list api-keys
    ;;
  status)
    curl -fsS http://127.0.0.1:8787/healthz || { echo "the edge does not answer on 127.0.0.1:8787" >&2; exit 1; }
    echo
    docker ps --filter name=ingest-edge --format '{{.Names}}  {{.Status}}' 2>/dev/null || true
    if [ -r "$SRC" ]; then echo "sources:"; grep -vE '^\s*(#|$)' "$SRC" | sed 's/^/  /'; fi
    ;;
  *)
    sed -n '6,21p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
