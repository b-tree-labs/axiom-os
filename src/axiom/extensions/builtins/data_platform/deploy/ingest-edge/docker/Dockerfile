# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# The ingest edge (ADR-177) as one container: push lanes /ingest, the
# downstream's export /edge, and /healthz. Nothing else is served.
FROM python:3.12-slim

ARG AXIOM_VERSION=0.67.5
RUN pip install --no-cache-dir "axiom-os-lm==${AXIOM_VERSION}" \
 && useradd --system --home /data --uid 10001 edge \
 && mkdir -p /data && chown edge /data

COPY entrypoint.sh /entrypoint.sh
USER edge
ENV AXI_STATE_DIR=/data/state \
    AXIOM_INGEST_OUTBOX_DIR=/data/outbox \
    AXIOM_GATE_API_KEYS_FILE=/config/gate-api-keys.json
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=4).status==200 else 1)"
ENTRYPOINT ["sh", "/entrypoint.sh"]
