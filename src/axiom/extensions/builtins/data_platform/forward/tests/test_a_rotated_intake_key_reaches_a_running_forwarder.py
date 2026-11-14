# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A key rotated in the vault reaches a forwarder that is already running (C-34).

``data forward --watch`` read this site's intake key from the vault once, at
start, and ran for days on that copy. A leaked key revoked at the intake and
replaced at the site (``secrets exposed``, which records the leak and rotates)
therefore left the forwarder presenting the revoked key until someone
restarted it. It held every batch (nothing was lost), but nothing moved.

Now the key is read from the vault again as soon as the intake refuses it,
and every few minutes besides. Proven here with the real pieces: an upstream
intake started with the shipped CLI, the site key stored and rotated with the
secrets CLI (file-backed vault, no OS keychain), and one ``data forward`` run
that is never restarted.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.forward.forwarder import IntakeTarget
from axiom.rag.ingest_router import Disposition

from ...agents.plinth.connectors import ConnectorConfig, save_connector
from ...bronze import FilesystemTabularBronzeSink, TabularBronzeWriter
from ...ingest_sink import PushRowBatch, TabularIngestSink
from ...ingest_sink.edge import EdgeOutbox

AXI = [sys.executable, "-c", "import sys; from axiom.axiom_cli import main; sys.exit(main())"]
SITE, SOURCE, KEY_NAME = "site-a", "site-a-src", "site-a-ingest"

pytest.importorskip("uvicorn")


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(args, env, *, stdin: str | None = None) -> subprocess.CompletedProcess:
    out = subprocess.run(AXI + args, env=env, input=stdin, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, (out.stdout[-800:], out.stderr[-1500:])
    return out


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(("AXIOM_", "AXI_"))}


@pytest.fixture
def upstream(tmp_path):
    """The upstream intake, in its own process, keys in a config file it does not own."""
    root = tmp_path / "upstream"
    keys = root / "config" / "gate-api-keys.json"
    keys.parent.mkdir(parents=True)
    env = dict(_clean_env(), AXI_STATE_DIR=str(root / "state"), AXIOM_INGEST_OUTBOX_DIR=str(root / "outbox"),
               AXIOM_GATE_API_KEYS_FILE=str(keys), AXIOM_EDGE_DOWNSTREAM="@up:org", AXI_DIAGNOSES_QUIET="1",
               AXIOM_ACTOR="@operator:org")
    _run(["data", "register", "--bronze-root", str(root / "bronze" / SOURCE), "--site", SITE,
          "--default-disposition", "allow", "--default-tier", "restricted", SOURCE, "push"], env)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    def issue() -> str:
        out = _run(["gate", "--json", "issue", "api-key", "--principal", f"@daq:{SITE}", "--site", SITE,
                    "--scope", "data_platform:invoke"], env)
        return json.loads(out.stdout)["value"]["token"]

    proc = subprocess.Popen(AXI + ["serve", "--profile", "ingest-edge", "--host", "127.0.0.1", "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(300):
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=2):
                break
        except OSError:
            assert proc.poll() is None, "the intake exited at start"
            time.sleep(0.2)
    else:
        raise AssertionError("the intake never answered /healthz")
    yield {"url": url, "keys": keys, "root": root, "issue": issue}
    proc.terminate()
    proc.wait(timeout=10)


def _remove_key(keys: Path, token: str) -> None:
    """What an operator does to the mounted config: drop the record, swap the file in."""
    key_id = token.split("_")[1]
    records = [r for r in json.loads(keys.read_text()) if r["key_id"] != key_id]
    tmp = keys.with_suffix(".new")
    tmp.write_text(json.dumps(records))
    os.replace(tmp, keys)


def _landed_upstream(root: Path) -> list[str]:
    rows = []
    for f in sorted((root / "bronze" / SOURCE / SOURCE / "_rows").rglob("*.jsonl")):
        rows += [json.loads(x)["row_hash"] for x in f.read_text().splitlines() if x.strip()]
    return rows


@pytest.fixture
def site(tmp_path, monkeypatch):
    """This site: its outbox and bronze on disk, its vault file-backed."""
    state = tmp_path / "site" / "state"
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    for name in ("DP1_RAG_DSN", "DATABASE_URL", "AXIOM_DB_URL"):
        monkeypatch.delenv(name, raising=False)
    bronze = tmp_path / "site" / "bronze"
    save_connector(ConnectorConfig(name=SOURCE, kind="push", bronze_root=str(bronze), site=SITE), state_dir=state)
    outbox = EdgeOutbox(tmp_path / "site" / "outbox")
    sink = TabularIngestSink(
        writer=TabularBronzeWriter(rules=[], sink=FilesystemTabularBronzeSink(root=bronze),
                                   default_disposition=Disposition.ALLOW, default_tier="restricted"),
        on_landed=outbox.record,
    )
    env = dict(_clean_env(), AXI_STATE_DIR=str(state), AXIOM_FOREIGN_SECRETS_BACKEND="file")

    def land(i: int) -> None:
        rows = [{"channel": "TC1", "ts": f"2026-10-08T00:{i:02d}:{k:02d}Z", "value": 20.0 + k} for k in range(5)]
        sink.ingest_rows(SOURCE, [PushRowBatch(item_id=f"run-{i}", schema_ref="s/v1", rows=rows,
                                               metadata={"site": SITE})])

    return {"state": state, "outbox": outbox, "env": env, "land": land}


def _status(state: Path) -> dict:
    try:
        return json.loads((state / "forward" / "status.json").read_text())
    except (OSError, ValueError):
        return {}


def _until(check, what: str, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        assert time.monotonic() < deadline, what
        time.sleep(0.25)


def test_one_forward_run_outlives_a_revoked_and_rotated_key(upstream, site):
    from ...cli import _build_ctx
    from ...skills import forward

    leaked = upstream["issue"]()
    _run(["secrets", "set", KEY_NAME], site["env"], stdin=leaked)
    for i in range(3):
        site["land"](i)

    params = {"intake_url": upstream["url"], "key_ref": f"file://{KEY_NAME}", "probe_source": SOURCE,
              "outbox_dir": str(site["outbox"].dir), "state_dir": str(site["state"]),
              "watch_s": 0.5, "max_passes": 100, "up_after": 1, "down_after": 1}
    result: dict = {}
    run = threading.Thread(target=lambda: result.update(r=forward.run(params, _build_ctx())), daemon=True)
    run.start()
    try:
        _until(lambda: _status(site["state"]).get("after") == 3, "the first three batches never reached the intake")

        # The leaked key is revoked at the intake; the site keeps landing data.
        _remove_key(upstream["keys"], leaked)
        site["land"](3)
        site["land"](4)
        _until(lambda: _status(site["state"]).get("key_refused"), "the forwarder never noticed the refusal")
        assert _status(site["state"])["after"] == 3  # held, not skipped

        # The runbook's step, at the site: record the leak and rotate the key.
        fresh = upstream["issue"]()
        _run(["secrets", "exposed", f"file://{KEY_NAME}", "--where", "transcript", "--strategy", "hitl",
              "--value", fresh], site["env"])
        _until(lambda: _status(site["state"]).get("after") == 5,
               "the rotated key never reached the running forwarder")
    finally:
        run.join(timeout=120)  # the run ends on its own after max_passes

    assert result["r"].ok, result["r"].errors
    assert _status(site["state"])["key_refused"] is None
    landed = _landed_upstream(upstream["root"])
    assert len(landed) == 25 and len(set(landed)) == 25  # five batches of five, once each
    exposures = (site["state"] / "secrets" / "exposures.jsonl").read_text().splitlines()
    assert [json.loads(x)["event"] for x in exposures] == ["exposure", "rotation"]


class _Intake(http.server.BaseHTTPRequestHandler):
    """Refuses every key but the current one, as an intake does."""

    accepted = "k-new"
    seen: list[str] = []

    def do_POST(self):  # noqa: N802 - http.server's name
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        type(self).seen.append(token)
        if token == self.accepted:
            self.send_response(200)
            body = b"{}"
        else:
            self.send_response(403)
            body = json.dumps({"error": {"code": "invalid_credential", "message": "no valid credential"}}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # quiet
        pass


def test_a_refused_key_is_read_again_at_once_and_an_accepted_one_only_on_the_interval():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Intake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        vault = ["k-old"]
        reads = []

        def provider():
            reads.append(vault[0])
            return vault[0]

        target = IntakeTarget(f"http://127.0.0.1:{server.server_port}", token_provider=provider,
                              token_ttl_s=3600, probe_source="s")
        body = b'{"source":"s","batches":[]}'
        assert target._post(body)[0] == 403 and target.key_refused
        vault[0] = "k-new"
        assert target._post(body)[0] == 200 and target.key_refused is None  # re-read on the refusal
        assert target._post(body)[0] == 200
        assert reads == ["k-old", "k-new"]  # accepted: cached for the interval, not re-read per request
        assert _Intake.seen[-3:] == ["k-old", "k-new", "k-new"]
    finally:
        server.shutdown()
