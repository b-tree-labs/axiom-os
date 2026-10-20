#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Bring up an ingest edge the way an operator would, push to it as a producer
over the host's network address, and pull it as a downstream (ADR-177).

Every step is the shipped CLI: `axi data register`, `axi gate issue`,
`axi serve --profile ingest-edge`, `axi data edge-pull`. Run in CI on a hosted
runner, it proves the edge works from outside any campus network. Exits
non-zero on any mismatch. No key is printed: tokens are read from `--json`
output into the environment of the child that needs them.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

AXI = [sys.executable, "-c", "import sys; from axiom.axiom_cli import main; sys.exit(main())"] if os.environ.get("AXI_FROM_MODULE") else ["axi"]
ROWS = 25
BATCHES = 4


def run(args: list[str], env: dict[str, str]) -> dict:
    out = subprocess.run(AXI + args, env=env, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        sys.exit(f"`axi {' '.join(args[:3])} ...` failed:\n{out.stderr[-2000:]}")
    return json.loads(out.stdout) if out.stdout.strip().startswith("{") else {}


def host_address() -> str:
    """The host's own network address, not loopback, so the request crosses
    a real interface the way a producer's would."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1; no packet is sent for UDP connect
        return s.getsockname()[0]


def http(method: str, url: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - local smoke
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="edge-smoke-"))
    base = {k: v for k, v in os.environ.items() if not k.startswith(("AXIOM_", "AXI_"))}
    edge_env = dict(base, AXI_STATE_DIR=str(work / "edge-state"),
                    AXIOM_INGEST_OUTBOX_DIR=str(work / "edge-outbox"),
                    AXIOM_GATE_API_KEYS_FILE=str(work / "edge-state" / "keys.json"),
                    AXIOM_EDGE_DOWNSTREAM="@downstream:org", AXI_DIAGNOSES_QUIET="1",
                    AXIOM_ACTOR="@edge-operator:org")

    run(["data", "register", "--bronze-root", str(work / "edge-bronze" / "site-a-src"), "--site", "site-a",
         "--default-disposition", "allow", "--default-tier", "restricted", "site-a-src", "push"], edge_env)
    producer = run(["gate", "--json", "issue", "api-key", "--principal", "@daq:site-a", "--site", "site-a",
                    "--scope", "data_platform:invoke"], edge_env)["value"]["token"]
    node = run(["gate", "--json", "issue", "api-key", "--principal", "@downstream:org",
                "--scope", "edge_export:read"], edge_env)["value"]["token"]

    port = 18787
    server = subprocess.Popen(AXI + ["serve", "--profile", "ingest-edge", "--host", "0.0.0.0", "--port", str(port)],
                              env=edge_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    url = f"http://{host_address()}:{port}"
    try:
        for _ in range(100):
            try:
                if http("GET", f"{url}/healthz")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            sys.exit("edge never answered /healthz:\n" + (server.stderr.read() if server.stderr else ""))

        sent = 0
        for b in range(BATCHES):
            rows = [{"channel": "C1", "ts": f"2026-10-06T00:{b:02d}:{i:02d}Z", "value": i, "unit": "1"} for i in range(ROWS)]
            status, body = http("POST", f"{url}/ingest/rows", producer, {
                "source": "site-a-src",
                "batches": [{"item_id": f"run-{b}", "schema_ref": "smoke/rows-v1", "rows": rows}]})
            assert status == 200 and body.get("rows_landed") == ROWS, (status, body)
            sent += ROWS
        # A replay lands nothing new.
        status, body = http("POST", f"{url}/ingest/rows", producer, {
            "source": "site-a-src",
            "batches": [{"item_id": "run-0", "schema_ref": "smoke/rows-v1",
                         "rows": [{"channel": "C1", "ts": f"2026-10-06T00:00:{i:02d}Z", "value": i, "unit": "1"} for i in range(ROWS)]}]})
        assert status == 200 and body.get("rows_landed") == 0, (status, body)

        # Refusals: no credential writes nothing; a producer cannot read the
        # export; nothing else is served.
        assert http("POST", f"{url}/ingest/rows", None, {"source": "site-a-src", "batches": []})[0] in (401, 403)
        assert http("GET", f"{url}/edge/outbox?after=0", producer)[0] == 403
        for path in ("/v1/models", "/gate/keys", "/classroom/x"):
            assert http("GET", url + path, producer)[0] in (403, 404), path

        down_env = dict(base, AXI_STATE_DIR=str(work / "down-state"),
                        EDGE_SMOKE_TOKEN=node, AXI_DIAGNOSES_QUIET="1",
                        AXIOM_ACTOR="@downstream-operator:org")
        run(["data", "register", "--bronze-root", str(work / "down-bronze" / "site-a-src"), "--site", "site-a",
             "--default-disposition", "allow", "--default-tier", "restricted", "site-a-src", "push"], down_env)
        run(["data", "register", "--bronze-root", str(work / "down-bronze" / "edge"),
             # Plain HTTP is accepted only on loopback; a real downstream uses
             # the edge's https:// name. The producer's pushes above crossed
             # the runner's network interface.
             "--credential-ref", "env://EDGE_SMOKE_TOKEN", "the-edge", "edge",
             "--edge-url", f"http://127.0.0.1:{port}"], down_env)
        first = run(["data", "--json", "edge-pull", "the-edge"], down_env)["value"]
        second = run(["data", "--json", "edge-pull", "the-edge"], down_env)["value"]
    finally:
        server.terminate()
        server.wait(timeout=10)

    assert first["records"] == BATCHES and first["rows_landed"] == sent, first
    assert second["records"] == 0, second
    print(f"edge smoke ok: {sent} rows pushed to {url}, {first['rows_landed']} pulled, rerun pulled {second['records']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
