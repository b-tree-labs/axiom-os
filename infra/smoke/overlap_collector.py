"""A stand-in collector that updates by overlap, for the chart's overlap proof.

It speaks the switch supervisor's protocol (ADR-182 D3): it is told its own
instance id (AXI_INSTANCE), keeps its own journal at <journal>@<instance>,
registers under <journal>.instances/, says it is ready on AXI_READY_FD once
its first reading has landed, and on SIGTERM finishes the push it holds and
leaves. Readings come from a clock both copies share (one every 0.2 s), so
during an overlap both copies send the same readings, as two copies of a real
collector reading one instrument would; the pipeline must keep each once.
"""

import json
import os
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path

URL, TOKEN = os.environ["EDGE_URL"], os.environ["PRODUCER_TOKEN"]
SOURCE = os.environ.get("SOURCE", "smoke-src")
INSTANCE = os.environ.get("AXI_INSTANCE", "solo")
BASE = Path(os.environ.get("JOURNAL", "/data/journal"))
JOURNAL = BASE.with_name(f"{BASE.name}@{INSTANCE}")
REGISTRY = BASE.with_name(f"{BASE.name}.instances")
JOURNAL.mkdir(parents=True, exist_ok=True)
REGISTRY.mkdir(parents=True, exist_ok=True)
(REGISTRY / f"{INSTANCE}.json").write_text(json.dumps({"pid": os.getpid(), "started": time.time()}))

stop = False
stop_at = None


def _term(*_):
    # Drain: send everything read up to now, then leave. The new copy has been
    # reading since before this, so between them nothing is missed.
    global stop, stop_at
    stop, stop_at = True, int(time.time() * 5)


signal.signal(signal.SIGTERM, _term)
signal.signal(signal.SIGUSR1, lambda *_: None)  # "you are the only copy": nothing to recover here


def push(tick: int) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(tick // 5)) + f".{(tick % 5) * 200:03d}Z"
    body = {
        "source": SOURCE,
        "batches": [
            {
                "item_id": f"tick-{tick}-{INSTANCE}",
                "schema_ref": "smoke/rows-v1",
                "rows": [{"channel": "C1", "ts": ts, "value": tick, "unit": "1"}],
            }
        ],
    }
    req = urllib.request.Request(
        URL + "/ingest/rows",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


for _ in range(300):  # the edge may still be starting; that is not a refusal
    try:
        if urllib.request.urlopen(URL + "/healthz", timeout=3).status == 200:
            break
    except OSError:
        time.sleep(1)
print(f"copy {INSTANCE} started", flush=True)
ready_fd = os.environ.pop("AXI_READY_FD", None)
refused = 0
reasons: list[str] = []
last = None
with open(JOURNAL / "ticks", "a") as sent:
    while not stop or (last is not None and last < stop_at):
        now = int(time.time() * 5) if not stop else min(int(time.time() * 5), stop_at)
        # Every tick since the last one sent, in order: a slow push delays
        # readings (as a journal would hold them) but never drops one.
        tick = now if last is None else last + 1
        if tick <= now:
            while True:
                try:
                    push(tick)
                    break
                except urllib.error.HTTPError as e:
                    # The edge answered and said no. A retry that later lands
                    # still counts: the proof is that nothing was refused.
                    refused += 1
                    reasons.append(f"http {e.code} tick {tick}")
                    print(f"refused: HTTP {e.code}: {e.reason}", flush=True)
                    time.sleep(0.2)
                except (urllib.error.URLError, TimeoutError, ConnectionError, InterruptedError) as e:
                    # SIGTERM arrives in the middle of a push when this copy
                    # drains. The syscall fails, the retry lands, and the edge
                    # did not refuse the reading. A dropped connection while
                    # this copy is still the reader is a refusal.
                    if not stop:
                        refused += 1
                        reasons.append(f"{type(e).__name__} tick {tick}")
                        print(f"refused: {type(e).__name__}: {e}", flush=True)
                    else:
                        print(f"interrupted while draining: {type(e).__name__}: {e}", flush=True)
                    time.sleep(0.2)
                except Exception as e:  # noqa: BLE001 - anything else is a refusal
                    refused += 1
                    reasons.append(f"{type(e).__name__} tick {tick}")
                    print(f"refused: {type(e).__name__}: {e}", flush=True)
                    time.sleep(0.2)
            sent.write(f"{tick}\n")
            sent.flush()
            last = tick
            if ready_fd is not None:
                os.write(int(ready_fd), b"R")
                os.close(int(ready_fd))
                ready_fd = None
        time.sleep(0.02)
(JOURNAL / "refused").write_text(str(refused))
(JOURNAL / "refusals").write_text("\n".join(reasons))
(REGISTRY / f"{INSTANCE}.json").unlink(missing_ok=True)
