# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every upload-artifact step sets a short retention-days.

The `dist` artifact (~23 MB) is uploaded on every CI run and consumed only by
later jobs of the SAME run, yet it inherited the 90-day default: ~470 copies,
7.7 GB, which is what filled the org's Actions storage quota.
"""
from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[2] / ".github" / "workflows"
MAX_DAYS = {"memory-benchmarks.yml": 90}  # benchmark history is kept on purpose
DEFAULT_MAX = 7


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
        days = w.get("retention-days")
        assert days is not None, f"{name}:{jid} uploads {w.get('name')!r} with the 90-day default"
        assert int(days) <= MAX_DAYS.get(name, DEFAULT_MAX), f"{name}:{jid} keeps {days} days"
