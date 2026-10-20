# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A served intake's 426 reaches the sender as something it can act on.

The earlier proof used a bare app. A served node wraps every refusal in one
error envelope, which turned the 426's structured body into a string, so a
sender holding its rows could not tell it was being asked to update. This runs
the intake as ``axi serve`` would compose it, in its own process.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform import compat
from axiom.extensions.builtins.data_platform.daq.consolidator import DAQConsolidator
from axiom.extensions.builtins.data_platform.daq.envelope import ConsolidatedRecord
from axiom.extensions.builtins.data_platform.daq.journal import DAQJournal
from axiom.extensions.builtins.data_platform.daq.transmitter import DAQTransmitter

_EDGE = """
import uvicorn, sys
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.registry import RouterRegistry
app = compose_app(profile="ingest-edge", registry=RouterRegistry(), bind_host="0.0.0.0")
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def intake(tmp_path):
    pytest.importorskip("uvicorn")
    from axiom.webauth import append_api_key_record, mint_api_key

    keys = tmp_path / "keys.json"
    token, rec = mint_api_key(principal="@daq:site-a", scopes=("data_platform:invoke",), site="site-a")
    append_api_key_record(keys, rec)
    src = str(Path(compat.__file__).resolve().parents[4])
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AXIOM_", "AXI_"))}
    env.update(PYTHONPATH=src + os.pathsep + env.get("PYTHONPATH", ""), AXI_STATE_DIR=str(tmp_path / "state"),
               AXIOM_INGEST_OUTBOX_DIR=str(tmp_path / "outbox"), AXIOM_GATE_API_KEYS_FILE=str(keys),
               AXIOM_MODE="dev", **{compat.MIN_CLIENT_ENV: "collector-pkg>=9.0"})
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", _EDGE, str(port)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(300):
        try:
            urllib.request.urlopen(url + "/healthz", timeout=2)
            break
        except OSError:
            if proc.poll() is not None:
                pytest.fail(proc.stdout.read().decode())
            time.sleep(0.2)
    yield url, token
    proc.terminate()
    proc.wait(timeout=10)


def test_the_envelope_keeps_the_structured_refusal(intake):
    url, token = intake
    body = json.dumps({"source": "s", "batches": [{"item_id": "x", "schema_ref": "s/v1", "rows": [{"a": 1}]}]})
    req = urllib.request.Request(url + "/ingest/rows", data=body.encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        compat.HEADER: "collector-pkg/1.0"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=30)
    assert exc.value.code == 426
    answer = json.loads(exc.value.read())
    assert answer["detail"]["code"] == "update_required"
    assert answer["detail"]["requirement"] == "collector-pkg>=9.0"
    assert answer["error"]["code"] == "update_required" and "collector-pkg>=9.0" in answer["error"]["message"]


def test_a_sender_against_a_served_intake_holds_and_says_update_required(intake, tmp_path):
    url, token = intake
    j = DAQJournal(tmp_path / "journal")
    cons = DAQConsolidator(journal=j, producer_id="site-a", feed="loop")
    for i in range(5):
        cons.consume(ConsolidatedRecord(schema_id="s/v1", ts=f"2026-10-08T00:00:0{i}Z", values={"V": float(i)}))
    tx = DAQTransmitter(face_url=url, source="site-a-src", schema_ref="s/v1", token=token, journal=j,
                        jitter=None, client_versions="collector-pkg/1.0")
    r = tx.pump()
    assert r.last_status == 426
    h = tx.health_details()
    assert h["connection"] == "update_required" and h["update_required"]["requirement"] == "collector-pkg>=9.0"
    assert h["consecutive_failures"] == 0 and j.lag(tx.cursor_name) == 5
    assert not tx.deadletter_path.exists()
