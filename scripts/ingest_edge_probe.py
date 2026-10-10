#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Probe a deployed ingest edge from outside any campus network (ADR-177).

    python scripts/ingest_edge_probe.py https://<edge-host>

Always checked, with no credential:
  - an anonymous push is refused by the edge itself (its JSON 401/403, not a
    proxy's 404 or 502), even claiming a loopback address;
  - the export refuses anonymous reads;
  - the host's own root page still answers (the edge shares a host and
    must not have taken it over).

With EDGE_PROBE_PRODUCER_KEY (a key for a probe-only site), one small batch
is pushed and must land, and a replay must land nothing. With
EDGE_PROBE_PULL_KEY as well (the downstream's key), the export must list it.
No key is printed. Exits non-zero on any failure.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


def http(method, url, token=None, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - operator-supplied https URL
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else {})
    except urllib.error.HTTPError as e:
        return e.code, {}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.exit(__doc__)
    base = argv[1].rstrip("/")
    if not base.startswith("https://"):
        sys.exit("the probe only talks to an https:// edge")
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("ok    " if ok else "FAIL  ") + what)
        if not ok:
            failures.append(what)

    body = {"source": "edge-probe-src", "batches": []}
    status, out = http("POST", f"{base}/ingest/rows", body=body)
    check(status in (401, 403) and isinstance(out, dict) and bool(out), "the edge answers, and refuses an anonymous push")
    check(http("POST", f"{base}/ingest/rows", body=body,
               headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"})[0] in (401, 403),
          "anonymous push claiming loopback refused")
    check(http("GET", f"{base}/edge/outbox?after=0")[0] in (401, 403), "anonymous export read refused")
    check(http("GET", f"{base}/")[0] < 500, "the host's own site still answers")

    producer = os.environ.get("EDGE_PROBE_PRODUCER_KEY")
    if producer:
        item = f"probe-{int(time.time())}"
        batch = {"source": "edge-probe-src", "batches": [{"item_id": item, "schema_ref": "edge-probe/rows-v1",
                 "rows": [{"channel": "probe", "ts": "2026-01-01T00:00:00Z", "value": 1, "unit": "1"}]}]}
        status, out = http("POST", f"{base}/ingest/rows", producer, batch)
        check(status == 200 and out.get("rows_landed") == 1, "a keyed push lands")
        status, out = http("POST", f"{base}/ingest/rows", producer, batch)
        check(status == 200 and out.get("rows_landed") == 0, "a replay lands nothing")
        puller = os.environ.get("EDGE_PROBE_PULL_KEY")
        if puller:
            status, out = http("GET", f"{base}/edge/outbox?after=0", puller)
            items = [r.get("item_id") for r in out.get("records", [])]
            check(status == 200 and item in items, "the downstream sees the batch in the export")
            check(http("GET", f"{base}/edge/outbox?after=0", producer)[0] == 403, "a producer cannot read the export")
    else:
        print("skip  keyed push (set EDGE_PROBE_PRODUCER_KEY to a probe-site key)")

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        return 1
    print("\nedge probe ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
