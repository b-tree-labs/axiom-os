# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A refused fleet push says why, and a push that keeps failing leaves a record (#1777).

Found in a node smoke test: the node's push had been refused every few minutes
for over a week. ``axi fleet report`` exited 1 and printed nothing, because the
failure was put in the result's actions and the CLI prints a failed result's
errors, which were empty. Only ``--json`` showed ``HTTP Error 403``.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error
from pathlib import Path

import pytest

from axiom.extensions.builtins.fleet import cli
from axiom.extensions.builtins.fleet.skills import report
from axiom.infra.skills import SkillContext, SkillRegistry

URL = "https://console.example/api/v1/fleet/reports"
TOKEN = "axk_secretvalue_never_printed"


def _refusal(status=403, message="credential scopes do not cover this resource", code="forbidden"):
    body = json.dumps({"error": {"code": code, "message": message, "request_id": "req_abc",
                                 "extension": "webapp"}}).encode()

    def transport(url, token, payload):
        raise urllib.error.HTTPError(url, status, "Forbidden", {}, io.BytesIO(body))

    return transport


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(report.PUSH_URL_ENV, URL)
    monkeypatch.setenv(report.PUSH_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(report.NODE_ID_ENV, "n-test")
    monkeypatch.setitem(report.COLLECTORS, "service_health", lambda: {})
    monkeypatch.setitem(report.COLLECTORS, "agents", lambda: {})


def _ctx(tmp_path: Path) -> SkillContext:
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path,
                        logger=logging.getLogger("axi.fleet"))


def test_a_scope_refusal_names_the_status_the_reason_and_the_fix(tmp_path):
    result = report.run({"_transport": _refusal()}, _ctx(tmp_path))
    assert result.ok is False
    message = " ".join(result.errors)
    assert "403" in message
    assert "credential scopes do not cover this resource" in message
    assert "fleet:invoke" in message
    assert result.unmet and result.unmet[0]["fix"]
    assert TOKEN not in json.dumps(result.errors) + json.dumps(result.unmet) + json.dumps(result.value)


def test_an_unaccepted_key_says_to_replace_it(tmp_path):
    transport = _refusal(message="no valid credential", code="invalid_credential")
    result = report.run({"_transport": transport}, _ctx(tmp_path))
    message = " ".join(result.errors)
    assert "403" in message and "no valid credential" in message
    assert report.PUSH_TOKEN_ENV in message


def test_an_unreachable_console_is_an_error_too(tmp_path):
    def transport(url, token, payload):
        raise ConnectionError("console unreachable")

    result = report.run({"_transport": transport}, _ctx(tmp_path))
    assert result.ok is False
    assert "console unreachable" in " ".join(result.errors)


def test_the_cli_prints_the_failure_on_stderr_and_exits_non_zero(monkeypatch, capsys):
    monkeypatch.setattr(report, "_default_transport", _refusal())
    assert cli.main(["report"]) == 1
    captured = capsys.readouterr()
    assert "403" in captured.err
    assert "fleet:invoke" in captured.err
    assert TOKEN not in captured.err + captured.out


def test_repeated_failure_is_recorded_and_escalated(tmp_path, caplog):
    ctx = _ctx(tmp_path)
    with caplog.at_level(logging.WARNING, logger="axi.fleet"):
        for _ in range(report.REPEATED_FAILURES):
            result = report.run({"_transport": _refusal()}, ctx)
    assert result.value["consecutive_failures"] == report.REPEATED_FAILURES
    record = json.loads((tmp_path / report.PUSH_RECORD).read_text())
    assert record["consecutive_failures"] == report.REPEATED_FAILURES
    assert record["last_status"] == 403
    assert record["first_failed_at"]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and "in a row" in errors[-1].getMessage()
    assert TOKEN not in caplog.text

    ok = report.run({"_transport": lambda url, token, body: {"accepted": 1}}, ctx)
    assert ok.ok is True
    record = json.loads((tmp_path / report.PUSH_RECORD).read_text())
    assert record["consecutive_failures"] == 0
    assert record["last_success_at"]
