# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A leaked key is revoked at the edge and replaced at the site, losing nothing (C-34).

Real processes: an ingest edge started with the shipped CLI (``axi serve
--profile ingest-edge``) reading its keys from a config file it does not own,
and a sender whose key lives in the vault, put there with ``secrets set``
(the command a site runs). Two steps, as an operator would take them:

1. Remove the leaked key's hash from the edge's keys file. The running edge
   refuses that key within the live-reload window (a minute) and keeps
   accepting every other key, with no restart.
2. Issue a new key and store it at the site with ``secrets set``. The running
   sender picks it up and delivers everything, including what it read while
   its key was refused: nothing lost, nothing twice.

Before this, a 401 dead-lettered each batch and advanced past it, so every
reading taken between the revocation and the new key was dropped from the
feed; and a sender holding a vault key read it once at start, so it kept
presenting the revoked one until restarted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.daq.consolidator import DAQConsolidator
from axiom.extensions.builtins.data_platform.daq.envelope import ConsolidatedRecord
from axiom.extensions.builtins.data_platform.daq.journal import DAQJournal
from axiom.extensions.builtins.data_platform.daq.transmitter import DAQTransmitter

AXI = [sys.executable, "-c", "import sys; from axiom.axiom_cli import main; sys.exit(main())"]
SITE, SOURCE, KEY_NAME = "site-a", "site-a-src", "site-a-ingest"
#: The edge reloads its keys file on change; a mounted config can take up to
#: a minute to reach the container. The refusal must land inside that.
RELOAD_WINDOW_S = 60

pytest.importorskip("uvicorn")


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(args, env, *, stdin: str | None = None) -> subprocess.CompletedProcess:
    out = subprocess.run(AXI + args, env=env, input=stdin, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, (out.stdout[-800:], out.stderr[-1500:])
    return out


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(("AXIOM_", "AXI_"))}


@pytest.fixture
def edge(tmp_path):
    root = tmp_path / "edge"
    keys = root / "config" / "gate-api-keys.json"
    keys.parent.mkdir(parents=True)
    env = dict(_clean_env(), AXI_STATE_DIR=str(root / "state"),
               AXIOM_INGEST_OUTBOX_DIR=str(root / "outbox"),
               AXIOM_GATE_API_KEYS_FILE=str(keys), AXI_DIAGNOSES_QUIET="1",
               AXIOM_ACTOR="@operator:org")
    for source, site in ((SOURCE, SITE), ("site-b-src", "site-b")):
        _run(["data", "register", "--bronze-root", str(root / "bronze" / source), "--site", site,
              "--default-disposition", "allow", "--default-tier", "restricted", source, "push"], env)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    def issue(site: str) -> str:
        out = _run(["gate", "--json", "issue", "api-key", "--principal", f"@daq:{site}",
                    "--site", site, "--scope", "data_platform:invoke"], env)
        return json.loads(out.stdout)["value"]["token"]

    leaked, other = issue(SITE), issue("site-b")
    proc = subprocess.Popen(AXI + ["serve", "--profile", "ingest-edge", "--host", "127.0.0.1",
                                   "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(300):
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=2):
                break
        except OSError:
            assert proc.poll() is None, "the edge exited at start"
            time.sleep(0.2)
    else:
        raise AssertionError("the edge never answered /healthz")
    yield {"url": url, "keys": keys, "root": root, "leaked": leaked, "other": other,
           "issue": issue}
    proc.terminate()
    proc.wait(timeout=10)


def _push(url: str, token: str, source: str, item: str) -> tuple[int, str]:
    body = {"source": source, "batches": [{"item_id": item, "schema_ref": "s/v1",
                                           "rows": [{"channel": "TC1", "ts": "2026-10-08T00:00:00Z",
                                                     "value": 1.0, "item": item}]}]}
    req = urllib.request.Request(url + "/ingest/rows", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}").get("error", {}).get("code", "")


def _remove_key(keys: Path, token: str) -> None:
    """What an operator does to the mounted config: drop the record, swap the file in."""
    key_id = token.split("_")[1]
    records = [r for r in json.loads(keys.read_text()) if r["key_id"] != key_id]
    assert len(records) == len(json.loads(keys.read_text())) - 1
    tmp = keys.with_suffix(".new")
    tmp.write_text(json.dumps(records))
    os.replace(tmp, keys)


def _landed(root: Path) -> list[str]:
    rows = []
    for f in sorted((root / "bronze" / SOURCE / SOURCE / "_rows").rglob("*.jsonl")):
        rows += [json.loads(x)["row_hash"] for x in f.read_text().splitlines() if x.strip()]
    return rows


def test_a_removed_key_is_refused_within_the_window_and_other_keys_still_land(edge):
    url = edge["url"]
    assert _push(url, edge["leaked"], SOURCE, "before")[0] == 200
    assert _push(url, edge["other"], "site-b-src", "before")[0] == 200

    _remove_key(edge["keys"], edge["leaked"])
    removed = time.monotonic()
    while _push(url, edge["leaked"], SOURCE, f"probe-{time.monotonic()}") != (403, "invalid_credential"):
        assert time.monotonic() - removed < RELOAD_WINDOW_S, "the removed key was never refused as a credential"
        time.sleep(1)
    took = time.monotonic() - removed

    assert _push(url, edge["other"], "site-b-src", "after")[0] == 200
    assert took < RELOAD_WINDOW_S


def test_a_new_key_stored_with_secrets_set_resumes_the_sender_with_nothing_lost_or_twice(
    edge, tmp_path, monkeypatch
):
    from axiom.extensions.builtins.secrets import SecretRef, resolve

    url = edge["url"]
    # The site's vault: file-backed, so the test never touches an OS keychain.
    site_env = dict(_clean_env(), AXI_STATE_DIR=str(tmp_path / "site"),
                    AXIOM_FOREIGN_SECRETS_BACKEND="file")
    for k in ("AXI_STATE_DIR", "AXIOM_FOREIGN_SECRETS_BACKEND"):
        monkeypatch.setenv(k, site_env[k])
    _run(["secrets", "set", KEY_NAME], site_env, stdin=edge["leaked"])

    def from_the_vault() -> str:
        with resolve(SecretRef.parse(f"file://{KEY_NAME}")) as secret:
            return secret.as_str().strip()

    now = [1000.0]
    journal = DAQJournal(tmp_path / "journal")
    reading = DAQConsolidator(journal=journal, producer_id=SITE, feed="loop")
    tx = DAQTransmitter(journal=journal, face_url=url, source=SOURCE, schema_ref="s/v1",
                        token_provider=from_the_vault, jitter=None, batch_size=5,
                        clock=lambda: now[0])

    def take(first: int, n: int) -> None:
        for i in range(first, first + n):
            reading.consume(ConsolidatedRecord(schema_id="s/v1", ts=f"2026-10-08T00:{i // 60:02d}:{i % 60:02d}Z",
                                               values={"TC1": float(i)}))

    def drain(limit: int = 20) -> None:
        for _ in range(limit):
            if journal.lag(tx.cursor_name) == 0:
                return
            tx.pump()
            now[0] += 120  # past any backoff, without sleeping through it

    take(0, 10)
    drain()
    assert journal.lag(tx.cursor_name) == 0 and tx.sent_total == 10

    # Step 1: the leaked key is removed at the edge; the site keeps reading.
    _remove_key(edge["keys"], edge["leaked"])
    take(10, 10)
    deadline = time.monotonic() + RELOAD_WINDOW_S
    while not tx.health_details()["key_refused"]:
        assert time.monotonic() < deadline, "the edge never refused the removed key"
        tx.pump()
        now[0] += 120
        time.sleep(0.5)
    assert journal.lag(tx.cursor_name) == 10  # held, every one of them
    assert not tx.deadletter_path.exists()
    health = tx.health_details()
    assert health["connection"] == "degraded" and "secrets set" in health["key_refused"]

    # Step 2: a new key, stored at the site the way an operator does it.
    take(20, 10)
    _run(["secrets", "set", KEY_NAME], site_env, stdin=edge["issue"](SITE))
    drain()

    assert journal.lag(tx.cursor_name) == 0
    assert tx.health_details()["key_refused"] is None
    assert not tx.deadletter_path.exists()
    landed = _landed(edge["root"])
    assert len(landed) == 30 and len(set(landed)) == 30
