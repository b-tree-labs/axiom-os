# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Kill switch, blast-radius caps, rate limits, and decide_act."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from axiom.extensions.builtins.hygiene.agency import (
    BlastCaps,
    apply_blast_caps,
    blast_caps,
    decide_act,
    rate_limit_allows,
    record_enforce_run,
    storage_enabled,
    storage_watch_only,
)
from axiom.extensions.builtins.settings.store import _DEFAULTS


NOW = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)


def test_shipped_defaults_are_agency_on():
    assert _DEFAULTS["hygiene.storage_watch_only"] is False
    assert _DEFAULTS["hygiene.storage_enabled"] is True
    assert _DEFAULTS["hygiene.storage_max_items_per_run"] == 200
    assert _DEFAULTS["hygiene.storage_max_gib_per_run"] == 2.0
    assert _DEFAULTS["hygiene.storage_max_runs_per_hour"] == 4
    assert _DEFAULTS["hygiene.retention.main_dist_days"] == 3
    assert _DEFAULTS["hygiene.retention.pr_dist_days"] == 3
    assert _DEFAULTS["hygiene.retention.logs_days"] == 14
    assert _DEFAULTS["hygiene.retention.coverage_days"] == 14
    assert _DEFAULTS["hygiene.retention.release_dist_days"] == 14
    assert _DEFAULTS["hygiene.retention.keep_main_dist"] == 2


def test_decide_act_default_and_overrides(monkeypatch):
    monkeypatch.delenv("AXIOM_HYGIENE_STORAGE_ENABLED", raising=False)
    monkeypatch.delenv("AXIOM_HYGIENE_STORAGE_WATCH_ONLY", raising=False)
    settings = lambda key, default=None: default  # noqa: E731
    assert decide_act({}, settings_get=settings) == (True, "default agency")
    assert decide_act({"dry_run": True, "enforce": True}, settings_get=settings)[0] is False
    assert decide_act({"enforce": True}, settings_get=settings) == (True, "enforce")
    assert decide_act({"surface": "mcp"}, settings_get=settings)[0] is False
    assert decide_act({"surface": "mcp", "enforce": True}, settings_get=settings)[0] is True
    assert decide_act({"surface": "agent_tool"}, settings_get=settings)[0] is False
    disabled = lambda key, default=None: False if key == "hygiene.storage_enabled" else default  # noqa: E731
    act, reason = decide_act({"enforce": True}, settings_get=disabled)
    assert act is False and "kill switch" in reason


def test_env_kill_switch_wins(monkeypatch):
    monkeypatch.setenv("AXIOM_HYGIENE_STORAGE_ENABLED", "false")
    assert storage_enabled(lambda key, default=None: True) is False
    monkeypatch.setenv("AXIOM_HYGIENE_STORAGE_ENABLED", "off")
    assert storage_enabled() is False
    monkeypatch.setenv("AXIOM_HYGIENE_STORAGE_WATCH_ONLY", "true")
    assert storage_watch_only() is True


def test_apply_blast_caps_prefers_larger_items():
    items = [
        SimpleNamespace(id="a", size_bytes=10),
        SimpleNamespace(id="b", size_bytes=50),
        SimpleNamespace(id="c", size_bytes=30),
        SimpleNamespace(id="d", size_bytes=5),
    ]
    chosen, deferred = apply_blast_caps(items, BlastCaps(max_items=2, max_gib=1))
    assert [i.id for i in chosen] == ["b", "c"]
    assert [i.id for i in deferred] == ["a", "d"]
    tiny = BlastCaps(max_items=10, max_gib=0)  # 0 bytes
    chosen, deferred = apply_blast_caps(items, tiny)
    assert chosen == []
    assert len(deferred) == 4


def test_aggressive_caps_at_85():
    caps = blast_caps(85, lambda key, default=None: default)
    assert caps.max_items == 500
    assert caps.max_gib == 4.0
    caps = blast_caps(70, lambda key, default=None: default)
    assert caps.max_items == 200
    assert caps.max_gib == 2.0


def test_rate_limit_trailing_hour(tmp_path):
    assert rate_limit_allows(tmp_path, now=NOW, max_runs_per_hour=2)[0] is True
    record_enforce_run(tmp_path, NOW - timedelta(minutes=10))
    record_enforce_run(tmp_path, NOW - timedelta(minutes=5))
    ok, reason = rate_limit_allows(tmp_path, now=NOW, max_runs_per_hour=2)
    assert ok is False
    assert "rate limit" in reason
    # A stamp older than an hour does not count.
    record_enforce_run(tmp_path, NOW - timedelta(hours=2))
    later = NOW + timedelta(hours=1)
    assert rate_limit_allows(tmp_path, now=later, max_runs_per_hour=2)[0] is True
