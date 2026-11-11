#!/bin/sh
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Refuse to start the edge when its data directory is short of space
# (ExecStartPre in axiom-ingest-edge.service). An edge that fills its disk
# refuses pushes producers then retry forever; better to fail loudly at start.
#   check-free-space.sh <dir> <min-free-GiB>
set -eu
dir="${1:?data directory}"
min_gib="${2:?minimum free GiB}"
avail_kib=$(df -Pk "$dir" | awk 'NR==2 {print $4}')
avail_gib=$((avail_kib / 1048576))
if [ "$avail_gib" -lt "$min_gib" ]; then
    echo "axiom-edge: $dir has ${avail_gib} GiB free; at least ${min_gib} GiB is required. Free space or raise the volume before starting." >&2
    exit 1
fi
echo "axiom-edge: $dir has ${avail_gib} GiB free (minimum ${min_gib} GiB)."
