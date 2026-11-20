# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Uninstall must confirm the unit is gone, not that the commands ran.

`SystemdProvider.uninstall` disabled the service and timer, unlinked the unit
files, ran `daemon-reload`, and returned True — without reading a single return
code. Its own comment states the stake: "Stop + disable both the service and
the timer so we don't leave an orphaned periodic schedule behind."

Nothing checked that it didn't. A disable that fails leaves a timer still
firing on a schedule nobody is watching, against unit files that have been
deleted, while the operator has been told the service is uninstalled. That is
worse than a failed uninstall reported honestly: the failure is now invisible
*and* the evidence has been removed.

Deleting the unit file is also not uninstalling. systemd holds the loaded unit
until a reload, so success has to be established after the reload, by asking.
"""

from __future__ import annotations


class _Svc:
    name = "probe-agent"
    interval_secs = 60


def _provider(monkeypatch, *, still_loaded: bool, disable_rc: int = 0):
    from axiom.infra import services as mod

    provider = mod.SystemdProvider()
    calls: list[list[str]] = []

    class _Result:
        def __init__(self, rc, out=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = "Failed to disable unit" if rc else ""

    def _run(cmd, *a, **kw):
        calls.append(list(cmd))
        if "disable" in cmd:
            return _Result(disable_rc)
        if "list-units" in cmd or "is-enabled" in cmd or "show" in cmd:
            return _Result(0, "loaded" if still_loaded else "")
        return _Result(0)

    monkeypatch.setattr(mod.subprocess, "run", _run)
    monkeypatch.setattr(provider, "_unit_path", lambda svc: _Missing())
    monkeypatch.setattr(provider, "_timer_path", lambda svc: _Missing())
    return provider, calls


class _Missing:
    def unlink(self, missing_ok=False):
        return None


def test_a_clean_uninstall_reports_success(monkeypatch):
    """Negative control: the ordinary path must still succeed."""
    provider, _ = _provider(monkeypatch, still_loaded=False)

    assert provider.uninstall(_Svc()) is True


def test_a_unit_still_loaded_after_uninstall_is_a_failure(monkeypatch):
    """The orphaned-timer case: files deleted, schedule still live."""
    provider, _ = _provider(monkeypatch, still_loaded=True)

    assert provider.uninstall(_Svc()) is False


def test_a_failed_disable_is_a_failure(monkeypatch):
    """Even when the subsequent probe comes back clean, a refused disable is
    not something to paper over."""
    provider, _ = _provider(monkeypatch, still_loaded=False, disable_rc=1)

    assert provider.uninstall(_Svc()) is False


def test_it_still_removes_the_unit_files_when_disable_fails(monkeypatch):
    """Reporting failure must not mean skipping cleanup — a half-removed
    service that reports failure is recoverable; one that reports failure and
    also left everything behind is not obviously different from doing nothing.
    """
    removed: list[str] = []

    from axiom.infra import services as mod

    provider = mod.SystemdProvider()

    class _Tracked:
        def __init__(self, label):
            self.label = label

        def unlink(self, missing_ok=False):
            removed.append(self.label)

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "nope"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _Result())
    monkeypatch.setattr(provider, "_unit_path", lambda svc: _Tracked("service"))
    monkeypatch.setattr(provider, "_timer_path", lambda svc: _Tracked("timer"))

    provider.uninstall(_Svc())

    assert set(removed) == {"service", "timer"}
