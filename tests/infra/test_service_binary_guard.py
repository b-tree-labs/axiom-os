# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Cross-platform launch-target guard for service installation.

Every OS service manager registers a unit/task/plist happily and only fails
*at launch* when the launch target can't run (systemd 203/EXEC, launchd spawn
error, Task Scheduler 0x2). That is a silent outage that surfaces at the next
reboot/logon. These tests pin the guard that refuses such an install up front,
on every provider — Linux, macOS, and Windows.
"""

from __future__ import annotations

import axiom.infra.services as services
from axiom.infra.services import (
    LaunchdProvider,
    ServiceDef,
    SubprocessProvider,
    SystemdProvider,
    WindowsTaskProvider,
    _resolve_exec_binary,
)


def _svc(binary: str) -> ServiceDef:
    return ServiceDef(
        name="probe-agent",
        binary=binary,
        args=["--json"],
        env={},
        interval_secs=300,
    )


# ---- _resolve_exec_binary --------------------------------------------------


def test_resolve_uses_which_for_bare_name(monkeypatch):
    monkeypatch.setattr(services.shutil, "which", lambda n: "/opt/bin/thing" if n == "thing" else None)
    assert _resolve_exec_binary(_svc("thing")) == "/opt/bin/thing"


def test_resolve_returns_none_for_unresolvable_bare_name(monkeypatch):
    monkeypatch.setattr(services.shutil, "which", lambda _n: None)
    assert _resolve_exec_binary(_svc("definitely-not-a-real-binary-xyz")) is None


def test_resolve_accepts_absolute_executable_file(monkeypatch, tmp_path):
    monkeypatch.setattr(services.shutil, "which", lambda _n: None)
    exe = tmp_path / "runme"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert _resolve_exec_binary(_svc(str(exe))) == str(exe)


def test_resolve_windows_file_needs_no_exec_bit(monkeypatch, tmp_path):
    # On Windows there is no executable bit; existence + PATHEXT decide. We
    # simulate the platform (NOT os.name — flipping that corrupts pathlib) so
    # a plain, non-+x file still resolves.
    monkeypatch.setattr(services.shutil, "which", lambda _n: None)
    monkeypatch.setattr(services.platform, "system", lambda: "Windows")
    exe = tmp_path / "runme.exe"
    exe.write_text("")  # no chmod +x
    assert _resolve_exec_binary(_svc(str(exe))) == str(exe)


def test_resolve_posix_rejects_non_executable_file(monkeypatch, tmp_path):
    monkeypatch.setattr(services.shutil, "which", lambda _n: None)
    monkeypatch.setattr(services.platform, "system", lambda: "Linux")
    plain = tmp_path / "data.txt"
    plain.write_text("not a program")
    plain.chmod(0o644)
    assert _resolve_exec_binary(_svc(str(plain))) is None


# ---- Provider install refuses an unrunnable target -------------------------


def test_systemd_install_refuses_missing_binary(monkeypatch):
    monkeypatch.setattr(services.shutil, "which", lambda _n: None)
    assert SystemdProvider().install(_svc("ghost-binary-xyz")) is False


def test_launchd_install_refuses_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(services.shutil, "which", lambda _n: None)
    prov = LaunchdProvider()
    # Point the plist path at a temp dir so we can prove nothing was written.
    plist_path = tmp_path / "ghost.plist"
    monkeypatch.setattr(prov, "_plist_path", lambda _svc: plist_path)
    assert prov.install(_svc("ghost-binary-xyz")) is False
    assert not plist_path.exists()


def test_windows_install_refuses_missing_binary_without_calling_schtasks(monkeypatch):
    monkeypatch.setattr(services.shutil, "which", lambda _n: None)

    def _boom(*_a, **_k):  # schtasks must never be reached
        raise AssertionError("schtasks was invoked despite an unrunnable binary")

    monkeypatch.setattr(services.subprocess, "run", _boom)
    assert WindowsTaskProvider().install(_svc("ghost-binary.exe")) is False


def test_windows_install_proceeds_when_binary_resolves(monkeypatch):
    monkeypatch.setattr(services.shutil, "which", lambda n: r"C:\bin\thing.exe" if n == "thing.exe" else None)
    calls: list[list[str]] = []

    class _OK:
        returncode = 0
        stdout = b""
        stderr = b""

    def _capture(cmd, *_a, **_k):
        calls.append(cmd)
        return _OK()

    monkeypatch.setattr(services.subprocess, "run", _capture)
    assert WindowsTaskProvider().install(_svc("thing.exe")) is True
    # The resolved absolute path — not the bare name — is what gets scheduled.
    assert any(r"C:\bin\thing.exe" in " ".join(c) for c in calls)


def test_subprocess_start_resolves_before_launch(monkeypatch):
    monkeypatch.setattr(services.shutil, "which", lambda n: "/opt/bin/thing" if n == "thing" else None)
    launched: dict[str, object] = {}

    def _fake_popen(argv, *_a, **_k):
        launched["argv"] = argv
        raise RuntimeError("stop before real spawn")

    monkeypatch.setattr(services.subprocess, "Popen", _fake_popen)
    SubprocessProvider().start(_svc("thing"))
    assert launched["argv"][0] == "/opt/bin/thing"
