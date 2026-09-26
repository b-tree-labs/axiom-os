# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""TDD tests for ``observability.verify`` skill."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from axiom.extensions.builtins.observability.skills import verify


def _proc(rc=0, out=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


def test_preflight_missing_helm(skill_ctx):
    with patch("shutil.which", return_value=None):
        r = verify.run({"phase": "preflight"}, skill_ctx)
    assert not r.ok
    assert any("helm" in e for e in r.errors)


def test_preflight_all_ok(skill_ctx):
    with patch("shutil.which", return_value="/bin/x"), \
         patch("subprocess.run", return_value=_proc(out="kind-axiom\n")):
        r = verify.run({"phase": "preflight", "namespace": "obs"}, skill_ctx)
    assert r.ok, r.errors


def test_postinstall_health_fail(skill_ctx, monkeypatch):
    """If /api/public/health returns non-200, verify fails."""

    class FakeResp:
        status_code = 503
        text = "down"

    def fake_get(*a, **kw):
        return FakeResp()

    monkeypatch.setattr(verify, "_http_get", lambda url, timeout=5: FakeResp())
    with patch("shutil.which", return_value="/bin/x"), \
         patch("subprocess.run", return_value=_proc(out="kind-axiom\n")):
        r = verify.run({"phase": "postinstall", "host": "http://obs.local"}, skill_ctx)
    assert not r.ok


def test_postinstall_health_ok_and_round_trip(skill_ctx, monkeypatch):
    class FakeResp:
        status_code = 200
        text = "ok"

    monkeypatch.setattr(verify, "_http_get", lambda url, timeout=5: FakeResp())

    # round-trip uses the in-memory provider, no network
    monkeypatch.setattr(verify, "_round_trip_trace", lambda host, public, secret: True)

    with patch("shutil.which", return_value="/bin/x"), \
         patch("subprocess.run", return_value=_proc(out="kind-axiom\n")):
        r = verify.run({
            "phase": "postinstall",
            "host": "http://obs.local",
            "public_key": "pk", "secret_key": "sk",
        }, skill_ctx)
    assert r.ok, r.errors


def test_unknown_phase_returns_error(skill_ctx):
    r = verify.run({"phase": "garbage"}, skill_ctx)
    assert not r.ok
