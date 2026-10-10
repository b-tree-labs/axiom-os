#!/bin/sh
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Start the per-user edge when the host gives the operator neither root nor
# systemd linger. It keeps running after logout, but it DOES NOT SURVIVE A
# REBOOT and nothing restarts it if it exits: check it after any host
# maintenance, and ask for `loginctl enable-linger <user>` to replace this.
#   run-edge-without-linger.sh [start|stop|status]
set -eu
ENV_FILE="${AXIOM_EDGE_ENV:-$HOME/.config/axiom-edge/edge.env}"
[ -r "$ENV_FILE" ] || { echo "no env file at $ENV_FILE" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a
PIDFILE="$AXIOM_EDGE_DATA_DIR/edge.pid"
LOG="$AXIOM_EDGE_DATA_DIR/edge.log"
running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
case "${1:-start}" in
  start)
    if running; then echo "edge already running (pid $(cat "$PIDFILE"))"; exit 0; fi
    "$HOME/axiom-edge/check-free-space.sh" "$AXIOM_EDGE_DATA_DIR" "$AXIOM_EDGE_MIN_FREE_GIB"
    nohup setsid "$HOME/axiom-edge/venv/bin/axi" serve --profile ingest-edge \
        --host 127.0.0.1 --port "$AXIOM_EDGE_PORT" >>"$LOG" 2>&1 </dev/null &
    echo $! >"$PIDFILE"
    echo "edge started (pid $!); log $LOG. This does not survive a reboot."
    ;;
  stop)
    if running; then kill "$(cat "$PIDFILE")"; rm -f "$PIDFILE"; echo "edge stopped"; else echo "edge not running"; fi
    ;;
  status)
    if running; then echo "edge running (pid $(cat "$PIDFILE"))"; else echo "edge not running"; exit 1; fi
    ;;
  *) echo "usage: $0 [start|stop|status]" >&2; exit 2 ;;
esac
