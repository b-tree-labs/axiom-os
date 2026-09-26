# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""fleet.report: opt-in gate, collector isolation, push outcome honesty."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.fleet.skills import report


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for var in (report.PUSH_URL_ENV, report.PUSH_TOKEN_ENV, report.NODE_ID_ENV):
        monkeypatch.delenv(var, raising=False)


def test_unconfigured_node_is_not_enrolled_and_exits_ok():
    """Probe rule: absence of opt-in is a fact, not a warning."""
    result = report.run({})
    assert result.ok is True
    assert result.value == {"enrolled": False}


def test_push_sends_collected_reports_with_declared_cadences():
    sent = {}

    def transport(url, token, body):
        sent.update({"url": url, "token": token, "body": body})
        return {"accepted": len(body["reports"])}

    result = report.run(
        {
            "push_url": "https://console.example/api/v1/fleet/reports",
            "push_token": "tok",
            "node_id": "n-test",
            "_transport": transport,
        }
    )
    assert result.ok is True
    assert sent["token"] == "tok"
    assert sent["body"]["node_id"] == "n-test"
    assert sent["body"]["cadences"] == report.DEFAULT_CADENCES
    kinds = {r["kind"] for r in sent["body"]["reports"]}
    assert "heartbeat" in kinds and "versions" in kinds
    assert result.value["accepted"] == result.value["sent"]


def test_broken_collector_drops_its_kind_never_blocks_the_push(monkeypatch):
    def boom():
        raise RuntimeError("collector exploded")

    monkeypatch.setitem(report.COLLECTORS, "service_health", boom)
    captured = {}

    def transport(url, token, body):
        captured["kinds"] = {r["kind"] for r in body["reports"]}
        return {"accepted": len(body["reports"])}

    result = report.run({"push_url": "https://c/api", "push_token": "t", "_transport": transport})
    assert result.ok is True
    assert "service_health" not in captured["kinds"]
    assert "heartbeat" in captured["kinds"]
    assert any("collector service_health failed" in n for n in result.value["notes"])


def test_failed_push_reports_failure_not_success():
    def transport(url, token, body):
        raise ConnectionError("console unreachable")

    result = report.run({"push_url": "https://c/api", "push_token": "t", "_transport": transport})
    assert result.ok is False
    assert "console unreachable" in result.value["error"]
