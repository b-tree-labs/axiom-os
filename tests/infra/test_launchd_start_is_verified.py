# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`start()` must report whether the service started, not that a command ran.

`LaunchdProvider.start` ran `launchctl load` and returned True unconditionally.
It never read the return code, and `capture_output=True` swallowed the error, so
a refused load — a malformed plist, a bad path, a disabled label — was reported
to every caller as a successful start.

The systemd provider alongside it already gets this right, and goes further: it
checks the return code and then confirms a periodic timer actually has a next
elapse, with a docstring saying "Surface that as a start failure instead of a
false 'running'." Two providers behind one interface, one honest and one not,
and the callers cannot tell which they have.

This is the same shape as a delivery receipt for an alert nobody can read, and
the same fix: report what happened, not that the call returned.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" and False, reason="provider logic is platform-agnostic here"
)


def _provider(monkeypatch, *, returncode: int, loaded_after: bool):
    """A LaunchdProvider whose launchctl calls are controlled by the test."""
    from axiom.infra import services as mod

    provider = mod.LaunchdProvider()
    calls: list[list[str]] = []

    class _Result:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = ""
            self.stderr = "Load failed: 5: Input/output error" if rc else ""

    def _run(cmd, *a, **kw):
        calls.append(list(cmd))
        return _Result(returncode)

    monkeypatch.setattr(mod.subprocess, "run", _run)
    monkeypatch.setattr(provider, "_is_loaded", lambda svc: loaded_after)
    return provider, calls


class _Svc:
    name = "probe-agent"
    interval_secs = 0


def test_a_refused_load_is_reported_as_a_failed_start(monkeypatch):
    """The bug: launchctl refuses, start() says it worked."""
    provider, _ = _provider(monkeypatch, returncode=1, loaded_after=False)
    monkeypatch.setattr(provider, "_plist_path", lambda svc: "/tmp/probe.plist")

    assert provider.start(_Svc()) is False


def test_a_successful_load_is_reported_as_success(monkeypatch):
    """Negative control: the fix must not make every start fail."""
    provider, _ = _provider(monkeypatch, returncode=0, loaded_after=True)
    monkeypatch.setattr(provider, "_plist_path", lambda svc: "/tmp/probe.plist")

    assert provider.start(_Svc()) is True


def test_a_zero_exit_that_did_not_load_is_still_a_failure(monkeypatch):
    """launchctl has historically exited 0 while loading nothing. The exit code
    is the claim; whether the agent is loaded is the fact."""
    provider, _ = _provider(monkeypatch, returncode=0, loaded_after=False)
    monkeypatch.setattr(provider, "_plist_path", lambda svc: "/tmp/probe.plist")

    assert provider.start(_Svc()) is False


def test_an_already_loaded_agent_short_circuits(monkeypatch):
    """The idempotence behaviour from issue #208 must survive: a loaded agent
    is not re-loaded, because that re-fires the macOS background-activity
    toast."""
    from axiom.infra import services as mod

    provider = mod.LaunchdProvider()
    monkeypatch.setattr(provider, "_is_loaded", lambda svc: True)
    ran: list[str] = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, *a, **kw: ran.append(cmd) or None)

    assert provider.start(_Svc()) is True
    assert ran == [], "an already-loaded agent must not be re-loaded"
