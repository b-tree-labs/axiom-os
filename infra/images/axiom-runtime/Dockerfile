# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# The one runtime image every node role runs. It holds only a base Python and
# the entrypoint: the packages live on the role's volume, installed once per
# version (the updater's layout), so a release never needs a new image.
FROM python:3.12-slim
# rclone: the forwarder's Box fallback (`axi data forward --box-remote`) when
# the upstream intake is down. Checked at build so a missing binary fails here.
RUN apt-get update \
 && apt-get install -y --no-install-recommends rclone ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && rclone version >/dev/null
# /data is a role's data; /pkg is a blue/green slot's own installed packages.
# Owned here so a fresh Docker volume mounted on either starts writable.
RUN useradd --system --uid 10001 --home /data axiom && mkdir -p /data /pkg && chown axiom /data /pkg
COPY entrypoint.sh /usr/local/bin/axiom-entrypoint
COPY edge-run.sh /usr/local/bin/edge-run
RUN chmod 0755 /usr/local/bin/axiom-entrypoint /usr/local/bin/edge-run
USER axiom
ENV AXIOM_ROOT=/data
ENTRYPOINT ["/usr/local/bin/axiom-entrypoint"]
