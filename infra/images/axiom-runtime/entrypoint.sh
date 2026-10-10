#!/bin/sh
# Contract (shared with the updater, update/swap.py):
#   $AXIOM_ROOT/venvs/<version>/  one venv per version, built in place, never moved
#   $AXIOM_ROOT/current -> venvs/<version>  relative link, switched atomically
# First boot installs $AXIOM_PACKAGES as $AXIOM_PACKAGES_VERSION once. A restart
# never reinstalls; an update (out of band) switches `current`, and the next
# exec runs the new version.
set -eu
ROOT="${AXIOM_ROOT:-/data}"
# Several processes may share one volume (a role, its init step, its
# companions), so the first-boot install is serialised by a lock on the volume.
# The others wait, then find the version already built.
bootstrap() {
  : "${AXIOM_PACKAGES:?set AXIOM_PACKAGES, e.g. axiom-os-lm==0.68.0}"
  : "${AXIOM_PACKAGES_VERSION:?set AXIOM_PACKAGES_VERSION}"
  V="$ROOT/venvs/$AXIOM_PACKAGES_VERSION"
  # Built at its final path (a venv is not relocatable). `.complete` is written
  # last, so a crash mid-install is rebuilt on the next boot, never used.
  if [ ! -e "$V/.complete" ]; then
    echo "bootstrap: installing $AXIOM_PACKAGES into $V" >&2
    rm -rf "$V"
    python -m venv "$V"
    # shellcheck disable=SC2086 # several packages may be listed
    "$V/bin/pip" install --no-cache-dir --quiet $AXIOM_PACKAGES
    touch "$V/.complete"
  fi
  ln -sfn "venvs/$AXIOM_PACKAGES_VERSION" "$ROOT/current.tmp"
  mv -T "$ROOT/current.tmp" "$ROOT/current"
}
if [ ! -e "$ROOT/current" ]; then
  exec 9>"$ROOT/.bootstrap.lock"
  flock 9
  [ -e "$ROOT/current" ] || bootstrap
  flock -u 9
  exec 9>&-
fi
running=$(readlink "$ROOT/current" 2>/dev/null || true)
if [ "$running" != "venvs/$AXIOM_PACKAGES_VERSION" ]; then
  # The node keeps the version it has: a changed pin is applied by an update
  # (ADR-179), never by a restart. Say so instead of advertising the new one.
  echo "warning: configured for $AXIOM_PACKAGES_VERSION but running ${running#venvs/}; apply the update to switch" >&2
  export AXIOM_PACKAGES_VERSION="${running#venvs/}"
fi
export PATH="$ROOT/current/bin:/usr/local/bin:$PATH"
# The node's declaration from the chart (role, functions, agent policy), where
# Axiom reads it: what the values say about agents is what this process obeys.
if [ -n "${AXIOM_NODE_TOML:-}" ] && [ -n "${AXIOM_NODE_CONFIG:-}" ]; then
  printf '%s\n' "$AXIOM_NODE_TOML" > "$AXIOM_NODE_CONFIG"
fi
# AXIOM_WAIT_FOR="host:port [host:port ...]": wait (up to AXIOM_WAIT_SECONDS,
# default 300) until each accepts a TCP connection. Service names are the same
# on Compose and Kubernetes, so one setting works on both.
if [ -n "${AXIOM_WAIT_FOR:-}" ]; then
  python - "$AXIOM_WAIT_FOR" "${AXIOM_WAIT_SECONDS:-300}" <<'PY'
import socket, sys, time
targets, limit = sys.argv[1].split(), float(sys.argv[2])
end = time.time() + limit
for t in targets:
    host, port = t.rsplit(":", 1)
    while True:
        try:
            socket.create_connection((host, int(port)), timeout=3).close()
            break
        except OSError:
            if time.time() > end:
                sys.exit(f"waited {limit:.0f}s for {t}; it never accepted a connection")
            time.sleep(2)
PY
fi
exec "$@"
