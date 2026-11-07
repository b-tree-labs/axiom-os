# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The tracker's freshness is earned by a verified sync, never asserted.

Pins the guarantee: the stamp advances only after a sync that ran against
every live feeder, read each to completion, and saw its writes land; a skipped,
refused, crashed, partial, or self-only sync leaves it where it was; and a
stale tracker says STALE (with since-when and why) in machine-readable form.
"""

from __future__ import annotations

import io
import json
import logging
import tomllib
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.program import freshness as fr
from axiom.extensions.builtins.program.model import ProgramData, ProgramError
from axiom.extensions.builtins.program.skills import _clients as clients
from axiom.extensions.builtins.program.skills import status, sync
from axiom.extensions.builtins.program.skills.sources import ConnectorReadiness
from axiom.infra.skills import SkillContext, SkillRegistry

T0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


def _record(**kw):
    base = {
        "last_attempt_at": "2026-10-14T12:00:00Z",
        "last_verified_sync_at": "2026-10-14T12:00:00Z",
        "last_attempt_ok": True,
        "consecutive_failures": 0,
        "live": True,
        "feeders": ["gitlab:tracker/7"],
    }
    base.update(kw)
    return base


class TestAssess:
    def test_fresh_within_two_heartbeats(self):
        block = fr.assess(_record(), now=T0 + timedelta(seconds=1800))
        assert block["state"] == "fresh" and block["fresh"] is True
        assert block["as_of"] == "2026-10-14T12:00:00Z"
        assert block["age_s"] == 1800 and block["stale_since"] is None

    def test_stale_one_second_past_the_window(self):
        block = fr.assess(_record(), now=T0 + timedelta(seconds=1801))
        assert block["state"] == "stale" and block["fresh"] is False
        assert block["stale_since"] == "2026-10-14T12:30:00Z"

    def test_stale_names_the_failure_when_the_clerk_is_failing(self):
        rec = _record(
            last_attempt_at="2026-10-14T13:00:00Z",
            last_attempt_ok=False,
            consecutive_failures=3,
            last_failure={"at": "2026-10-14T13:00:00Z", "reason": "gitlab: authentication failed"},
        )
        block = fr.assess(rec, now=T0 + timedelta(hours=2))
        assert block["state"] == "stale"
        assert "3 consecutive" in block["reason"] and "authentication failed" in block["reason"]

    def test_stale_names_silence_when_the_clerk_stopped(self):
        block = fr.assess(_record(), now=T0 + timedelta(hours=3))
        assert block["state"] == "stale"
        assert "not running" in block["reason"]

    def test_never_when_no_record(self):
        block = fr.assess({}, now=T0)
        assert block["state"] == "never" and block["fresh"] is False and block["as_of"] is None

    def test_never_with_failures_only_says_why(self):
        rec = {
            "last_attempt_at": "2026-10-14T12:00:00Z",
            "live": True,
            "last_attempt_ok": False,
            "consecutive_failures": 1,
            "last_failure": {"at": "2026-10-14T12:00:00Z", "reason": "host unreachable"},
        }
        block = fr.assess(rec, now=T0)
        assert block["state"] == "never" and "host unreachable" in block["reason"]

    def test_unfed_when_only_self_reconciles_ran(self):
        rec = {"last_attempt_at": "2026-10-14T12:00:00Z", "live": False, "last_attempt_ok": None}
        block = fr.assess(rec, now=T0)
        assert block["state"] == "unfed" and block["fresh"] is False

    def test_window_is_configurable_in_the_data_file(self):
        program = {"freshness": {"interval_s": 60, "stale_after_s": 100}}
        assert (
            fr.assess(_record(), program=program, now=T0 + timedelta(seconds=100))["state"]
            == "fresh"
        )
        assert (
            fr.assess(_record(), program=program, now=T0 + timedelta(seconds=101))["state"]
            == "stale"
        )

    def test_garbage_config_falls_back_to_the_defaults(self):
        program = {"freshness": {"interval_s": "soon", "stale_after_s": -5}}
        assert fr.thresholds(program) == (900, 1800)

    def test_a_corrupt_timestamp_is_no_evidence_never_fresh(self):
        block = fr.assess(_record(last_verified_sync_at="yesterday-ish"), now=T0)
        assert block["fresh"] is False

    def test_default_interval_is_the_manifest_heartbeat(self):
        manifest = Path(fr.__file__).with_name("axiom-extension.toml")
        agent = tomllib.loads(manifest.read_text(encoding="utf-8"))["agent"]
        assert agent["heartbeat_interval"] == fr.DEFAULT_INTERVAL_S


# ---------------------------------------------------------------------------
# the stamp moves only on a verified sync
# ---------------------------------------------------------------------------


@pytest.fixture
def node(tmp_path, data_dict):
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    data_dict["program"]["tracker"] = {
        "kind": "gitlab",
        "host": "tracker.example.org",
        "project_id": 7,
    }
    (state / "program" / "data.json").write_text(json.dumps(data_dict, indent=1), encoding="utf-8")
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.freshness"),
        user_prompt=None,
        surface="cli",
    )


class Feeder:
    def __init__(self, node, origin="gitlab:tracker/7", *, readiness=None, boom=None):
        self.origin = origin
        self._node = node
        self._readiness = readiness
        self._boom = boom

    def verify(self):
        return self._readiness or ConnectorReadiness(self.origin, True, True, True, "ok")

    def load(self):
        if self._boom is not None:
            raise self._boom
        raw = json.loads((self._node.state_dir / "program" / "data.json").read_text())
        raw["capture"] = {
            "source": self.origin,
            "system": "gitlab",
            "verified": True,
            "findings": [],
        }
        return ProgramData(raw=raw)


def _stamp(node):
    return fr.read_record(node.state_dir).get("last_verified_sync_at")


class TestTheStampIsEarned:
    def test_a_verified_sync_advances_it(self, node):
        result = sync.run({"_sources": [Feeder(node)]}, node)
        assert result.ok and result.value["verified"] is True
        assert _stamp(node) is not None
        assert result.value["freshness"]["state"] == "fresh"
        assert fr.freshness_path(node.state_dir).exists()

    def test_an_unverified_feeder_does_not(self, node):
        down = ConnectorReadiness("gitlab:tracker/7", False, False, False, "host unreachable")
        result = sync.run({"_sources": [Feeder(node, readiness=down)]}, node)
        assert result.ok and result.value["verified"] is False
        assert _stamp(node) is None
        record = fr.read_record(node.state_dir)
        assert (
            record["consecutive_failures"] == 1
            and "host unreachable" in record["last_failure"]["reason"]
        )

    def test_a_failed_sync_leaves_an_earned_stamp_alone(self, node):
        sync.run({"_sources": [Feeder(node)]}, node)
        earned = _stamp(node)
        down = ConnectorReadiness("gitlab:tracker/7", True, False, False, "authentication failed")
        sync.run({"_sources": [Feeder(node, readiness=down)]}, node)
        sync.run({"_sources": [Feeder(node, readiness=down)]}, node)
        record = fr.read_record(node.state_dir)
        assert record["last_verified_sync_at"] == earned
        assert record["consecutive_failures"] == 2 and record["last_attempt_ok"] is False

    def test_the_next_good_sync_clears_the_failures(self, node):
        down = ConnectorReadiness("gitlab:tracker/7", False, False, False, "host unreachable")
        sync.run({"_sources": [Feeder(node, readiness=down)]}, node)
        sync.run({"_sources": [Feeder(node)]}, node)
        record = fr.read_record(node.state_dir)
        assert record["consecutive_failures"] == 0 and record["last_attempt_ok"] is True

    def test_a_read_that_raises_program_error_does_not(self, node):
        result = sync.run(
            {
                "_sources": [
                    Feeder(node, boom=clients.TrackerReadError("tracker read returned HTTP 502"))
                ]
            },
            node,
        )
        assert not result.ok
        assert _stamp(node) is None
        assert "HTTP 502" in fr.read_record(node.state_dir)["last_failure"]["reason"]

    def test_an_unexpected_crash_does_not_and_still_raises(self, node):
        with pytest.raises(RuntimeError):
            sync.run({"_sources": [Feeder(node, boom=RuntimeError("secret-bearing detail"))]}, node)
        record = fr.read_record(node.state_dir)
        assert "last_verified_sync_at" not in record
        assert "secret-bearing detail" not in json.dumps(record)

    def test_a_partial_fanout_does_not(self, node):
        down = ConnectorReadiness("github:o/r", False, False, False, "host unreachable")
        result = sync.run(
            {"_sources": [Feeder(node), Feeder(node, "github:o/r", readiness=down)]}, node
        )
        assert result.value["verified"] is False
        assert _stamp(node) is None

    def test_a_write_that_does_not_read_back_does_not(self, node, monkeypatch):
        monkeypatch.setattr(sync, "_landed", lambda *a, **k: False)
        result = sync.run({"_sources": [Feeder(node)]}, node)
        assert result.value["verified"] is False and _stamp(node) is None
        assert "read back" in fr.read_record(node.state_dir)["last_failure"]["reason"]

    def test_a_self_reconcile_earns_nothing(self, node):
        result = sync.run({}, node)
        assert result.ok
        assert _stamp(node) is None
        assert fr.current(node.state_dir)["state"] == "unfed"


class TestStatusSurface:
    def test_scope_freshness_is_machine_readable(self, node):
        sync.run({"_sources": [Feeder(node)]}, node)
        result = status.run({"scope": "freshness"}, node)
        assert result.ok
        block = result.value["freshness"]
        assert (
            block["state"] == "fresh"
            and block["interval_s"] == 900
            and block["stale_after_s"] == 1800
        )
        json.dumps(result.value)

    def test_stale_is_reported_not_hidden(self, node):
        long_ago = datetime.now(UTC) - timedelta(hours=5)
        fr.record_attempt(
            node.state_dir, verified=True, live=True, feeders=["gitlab:x"], now=long_ago
        )
        block = status.run({"scope": "freshness"}, node).value["freshness"]
        assert block["state"] == "stale" and block["fresh"] is False
        assert block["stale_since"] and block["reason"]

    def test_freshness_answers_even_when_the_data_file_is_corrupt(self, node):
        (node.state_dir / "program" / "data.json").write_text("{not json", encoding="utf-8")
        result = status.run({"scope": "freshness"}, node)
        assert result.ok and result.value["freshness"]["state"] == "never"

    def test_every_scope_carries_the_block(self, node):
        assert status.run({"scope": "schedule"}, node).value["freshness"]["state"] == "never"

    def test_a_corrupt_record_is_never_fresh(self, node):
        fr.freshness_path(node.state_dir).write_text("[[[", encoding="utf-8")
        assert status.run({"scope": "freshness"}, node).value["freshness"]["fresh"] is False


# ---------------------------------------------------------------------------
# a failed read is not "nothing changed"
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, body, status=200, next_page=""):
        self.status = status
        self._body = json.dumps(body).encode()
        self.headers = {"X-Next-Page": next_page}

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestStrictPaginatedReads:
    def test_follows_every_page(self, monkeypatch):
        pages = {
            "1": _Resp([{"iid": 1}], next_page="2"),
            "2": _Resp([{"iid": 2}], next_page="3"),
            "3": _Resp([{"iid": 3}]),
        }

        def fake(request, timeout=0):
            return pages[request.full_url.rsplit("page=", 1)[1]]

        monkeypatch.setattr("urllib.request.urlopen", fake)
        got = clients._get_all_pages("https://t.example/api/v4/projects/7/issues?scope=all", {})
        assert [i["iid"] for i in got] == [1, 2, 3]

    def test_a_failure_on_a_later_page_raises_not_truncates(self, monkeypatch):
        calls = []

        def fake(request, timeout=0):
            calls.append(request.full_url)
            if len(calls) == 1:
                return _Resp([{"iid": 1}], next_page="2")
            raise urllib.error.URLError("boom")

        monkeypatch.setattr("urllib.request.urlopen", fake)
        with pytest.raises(clients.TrackerReadError):
            clients._get_all_pages("https://t.example/x", {})

    def test_http_error_names_the_status_not_the_body(self, monkeypatch):
        def fake(request, timeout=0):
            raise urllib.error.HTTPError(
                request.full_url, 502, "bad", {}, io.BytesIO(b"glpat-LEAK")
            )

        monkeypatch.setattr("urllib.request.urlopen", fake)
        with pytest.raises(clients.TrackerReadError) as err:
            clients._get_all_pages("https://t.example/x", {})
        assert "502" in str(err.value) and "glpat" not in str(err.value)

    def test_a_non_list_body_raises(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen", lambda r, timeout=0: _Resp({"message": "nope"})
        )
        with pytest.raises(clients.TrackerReadError):
            clients._get_all_pages("https://t.example/x", {})

    def test_a_read_error_is_a_program_error_so_sync_refuses_it(self):
        assert issubclass(clients.TrackerReadError, ProgramError)
