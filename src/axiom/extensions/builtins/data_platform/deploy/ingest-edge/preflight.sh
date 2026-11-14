#!/bin/sh
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Read-only preflight for installing an ingest edge on a host that already
# serves a site. Run it first, over SSH, as your own user:
#     sh preflight.sh <public-host-name>
# It reads; it never writes, installs, restarts or changes anything.
# Ends with GO or NO-GO and the reasons.
set -u
HOST="${1:?the public name the edge will be served under}"
PORT="${EDGE_PORT:-8787}"
MIN_GIB="${EDGE_MIN_FREE_GIB:-50}"
NOGO=""
WARN=""
nogo() { NOGO="${NOGO}
  - $1"; }
warn() { WARN="${WARN}
  - $1"; }
section() { printf '\n== %s\n' "$1"; }

section "Operating system"
if [ -r /etc/os-release ]; then . /etc/os-release; echo "${PRETTY_NAME:-unknown}"; else echo "unknown"; fi
command -v systemctl >/dev/null 2>&1 && echo "systemd: yes" || nogo "no systemd (the edge ships as a systemd unit)"

section "Python"
if command -v python3 >/dev/null 2>&1; then
    v=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
    echo "python3 $v"
    python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)' || nogo "python3 $v is older than 3.11"
    python3 -c 'import venv, ensurepip' 2>/dev/null && echo "venv: yes" || warn "python3 venv/ensurepip missing (Debian/Ubuntu: the python3-venv package)"
else
    nogo "no python3"
fi

section "Disk (need ${MIN_GIB} GiB free for /var/lib/axiom-edge)"
best=""
for d in /var/lib /opt /srv /data /home "$HOME"; do
    [ -d "$d" ] || continue
    kib=$(df -Pk "$d" | awk 'NR==2 {print $4}'); gib=$((kib / 1048576))
    mnt=$(df -P "$d" | awk 'NR==2 {print $6}')
    echo "$d: ${gib} GiB free (on $mnt)"
    [ "$d" = /var/lib ] && varlib=$gib
    if [ "$gib" -ge "$MIN_GIB" ] && [ -z "$best" ]; then best=$d; fi
done
if [ "${varlib:-0}" -lt "$MIN_GIB" ]; then
    if [ -n "$best" ]; then warn "/var/lib has less than ${MIN_GIB} GiB; put /var/lib/axiom-edge on $best (a bind mount or symlink)"
    else nogo "no candidate path has ${MIN_GIB} GiB free"; fi
fi

section "nginx"
if command -v nginx >/dev/null 2>&1; then
    nginx -v 2>&1
    files=$(grep -rlE "server_name[^;]*${HOST}" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null)
    if [ -n "$files" ]; then
        echo "server block for ${HOST}:"; for f in $files; do echo "  $f"; grep -nE "listen|server_name|location|include" "$f" | sed 's/^/    /'; done
        grep -qE "location[[:space:]]+[=~^]*[[:space:]]*/(ingest|edge)" $files && nogo "the ${HOST} server block already has an /ingest or /edge location"
    else
        warn "no readable server block names ${HOST} in /etc/nginx/sites-enabled or conf.d (try: sudo nginx -T | grep -n server_name)"
    fi
    [ -d /etc/nginx/snippets ] && echo "snippets dir: /etc/nginx/snippets" || warn "no /etc/nginx/snippets (the snippet can live beside the site file instead)"
else
    nogo "no nginx on PATH"
fi

section "Listening sockets"
if command -v ss >/dev/null 2>&1; then
    ss -Htln 2>/dev/null | awk '{print $4}' | sort -u | sed 's/^/  /'
    public=$(ss -Htln 2>/dev/null | awk '{print $4}' | grep -vE '^(127\.|\[::1\]|::1)' | sed -E 's/.*:([0-9]+)$/\1/' | sort -un | tr '\n' ' ')
    echo "non-loopback ports: ${public:-none}"
    for p in $public; do case "$p" in 22|80|443) ;; *) warn "port $p listens on a public address (only 443, and SSH, should face the internet)";; esac; done
    ss -Htln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}\$" && nogo "port ${PORT} is already in use (set EDGE_PORT and change the unit and snippet together)"
else
    warn "no ss; cannot list listening ports"
fi

section "Per-user service (no root needed)"
if systemctl --user show-environment >/dev/null 2>&1; then echo "user systemd: available"; else warn "no user systemd manager (systemctl --user); only run-edge-without-linger.sh will work"; fi
linger=$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo unknown)
echo "linger for $(id -un): $linger"
[ "$linger" = yes ] || warn "linger is off: a per-user edge stops at logout and does not start at boot until an administrator runs: loginctl enable-linger $(id -un)"

section "Users and services"
id axiom-edge >/dev/null 2>&1 && warn "user axiom-edge already exists" || echo "user axiom-edge: not present (will be created)"
systemctl list-unit-files 2>/dev/null | grep -q '^axiom-ingest-edge' && warn "unit axiom-ingest-edge already installed" || echo "unit axiom-ingest-edge: not present"
echo "running services:"; systemctl list-units --type=service --state=running --no-legend 2>/dev/null | awk '{print "  "$1}'
sudo -n true 2>/dev/null && echo "sudo: available without a prompt" || warn "sudo needs a password or is not granted (install steps need it)"

section "Outbound"
if command -v curl >/dev/null 2>&1; then
    c=$(curl -s -o /dev/null -w '%{http_code}' -m 10 https://pypi.org/simple/axiom-os-lm/)
    [ "$c" = 200 ] && echo "pypi.org: reachable" || nogo "cannot reach pypi.org (HTTP $c)"
else
    warn "no curl; outbound not checked"
fi

printf '\n== Result\n'
[ -n "$WARN" ] && printf 'Warnings:%s\n' "$WARN"
if [ -n "$NOGO" ]; then printf 'NO-GO:%s\n' "$NOGO"; exit 1; fi
echo "GO"
