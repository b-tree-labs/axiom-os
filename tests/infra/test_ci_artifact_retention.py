# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every upload-artifact step sets retention-days, driven by a repo variable.

TIDY enforces per-class defaults (main/PR dist 3 days, logs/coverage/release
dist 14 days, benchmarks kept). GitHub `retention-days` is the hard ceiling
so a RIVET `keep` can outlive the class default only while this still holds.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[2] / ".github" / "workflows"
# Ceilings: the numeric fallback in vars.AXIOM_* || N, or a literal.
MAX_DAYS = {"memory-benchmarks.yml": 90}
DEFAULT_MAX = 14

_FALLBACK = re.compile(r"\|\|\s*(\d+)")


def _days(raw) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if text.isdigit():
        return int(text)
    found = [int(n) for n in _FALLBACK.findall(text)]
    if found:
        return max(found)
    return None


def _uploads():
    for f in sorted(WF.glob("*.yml")):
        for jid, job in (yaml.safe_load(f.read_text()).get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if "actions/upload-artifact" in str(step.get("uses", "")):
                    yield f.name, jid, step.get("with") or {}


def test_every_upload_sets_short_retention():
    found = list(_uploads())
    assert found
    for name, jid, w in found:
        raw = w.get("retention-days")
        assert raw is not None, f"{name}:{jid} uploads {w.get('name')!r} with the 90-day default"
        days = _days(raw)
        assert days is not None, f"{name}:{jid} retention-days is not a number or vars.* || N: {raw!r}"
        assert days <= MAX_DAYS.get(name, DEFAULT_MAX), f"{name}:{jid} keeps {days} days"


def test_dist_uploads_use_repo_variable_with_sensible_fallback():
    dists = [(name, jid, w) for name, jid, w in _uploads() if w.get("name") == "dist"]
    assert dists, "expected at least one dist upload-artifact step"
    for name, jid, w in dists:
        raw = str(w.get("retention-days") or "")
        assert "vars." in raw and "RETENTION_DAYS" in raw, (
            f"{name}:{jid} dist retention-days should use a repo variable, got {raw!r}"
        )
        days = _days(raw)
        assert days is not None
        if name == "publish.yml":
            assert "AXIOM_RELEASE_DIST_RETENTION_DAYS" in raw
            assert days == 14
        else:
            assert "AXIOM_DIST_RETENTION_DAYS" in raw
            # ci.yml also names the release fallback for tag builds
            assert 3 in [int(n) for n in _FALLBACK.findall(raw)]
