"""A stand-in collector for the chain smoke: serve the newest value on the realtime
point (memory only), push N batches of rows to the edge, then keep publishing."""

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

LATEST = {"channel": "C1", "value": None, "ts": None}


class Realtime(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(LATEST).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


threading.Thread(
    target=HTTPServer(
        ("0.0.0.0", int(os.environ.get("AXIOM_REALTIME_PORT", "9100"))), Realtime
    ).serve_forever,
    daemon=True,
).start()
url, tok, src = (
    os.environ["EDGE_URL"],
    os.environ["PRODUCER_TOKEN"],
    os.environ.get("SOURCE", "smoke-src"),
)
batches, rows = int(os.environ.get("BATCHES", "4")), int(os.environ.get("ROWS", "25"))
for _ in range(120):
    try:
        if urllib.request.urlopen(url + "/healthz", timeout=3).status == 200:
            break
    except OSError:
        time.sleep(2)
if os.environ.get("MODE") == "continuous":
    # Push a batch every INTERVAL seconds for DURATION seconds, as a collector
    # does, across whatever deploys happen meanwhile. Every failed attempt is
    # counted (a deploy must cause none); the batch is retried until it lands,
    # so the downstream count still shows any loss or duplication.
    interval, duration = (
        float(os.environ.get("INTERVAL", "0.2")),
        float(os.environ.get("DURATION", "120")),
    )
    sent = refused = 0
    b, end = 0, time.time() + duration
    while time.time() < end:
        body = {
            "source": src,
            "batches": [
                {
                    "item_id": f"live-{b}",
                    "schema_ref": "smoke/rows-v1",
                    "rows": [
                        {
                            "channel": "C1",
                            "ts": f"2026-10-09T{b // 3600 % 24:02d}:{b // 60 % 60:02d}:{b % 60:02d}.{i:03d}Z",
                            "value": i,
                            "unit": "1",
                        }
                        for i in range(rows)
                    ],
                }
            ],
        }
        while True:
            req = urllib.request.Request(
                url + "/ingest/rows",
                data=json.dumps(body).encode(),
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    if r.status == 200:
                        sent += json.loads(r.read()).get("rows_landed", 0)
                        break
                    refused += 1
            except Exception as e:  # noqa: BLE001 - every kind of refusal counts
                refused += 1
                print(f"refused: {type(e).__name__}: {e}", flush=True)
            time.sleep(0.5)
        b += 1
        if b % 50 == 0:
            print(f"progress batches={b} sent={sent} refused={refused}", flush=True)
        time.sleep(interval)
    print("summary " + json.dumps({"batches": b, "sent": sent, "refused": refused}), flush=True)
    while True:
        time.sleep(3600)

landed = 0
for b in range(batches):
    body = {
        "source": src,
        "batches": [
            {
                "item_id": f"smoke-{b}",
                "schema_ref": "smoke/rows-v1",
                "rows": [
                    {
                        "channel": "C1",
                        "ts": f"2026-10-08T00:{b:02d}:{i:02d}Z",
                        "value": i,
                        "unit": "1",
                    }
                    for i in range(rows)
                ],
            }
        ],
    }
    req = urllib.request.Request(
        url + "/ingest/rows",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        landed += json.loads(r.read()).get("rows_landed", 0)
print(f"pushed {landed}", flush=True)
n = 0
while True:
    n += 1
    LATEST.update(value=n, ts=time.time())
    time.sleep(1)
