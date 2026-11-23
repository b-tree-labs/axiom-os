# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Tests for the node-reliability doctor checks (services / GPU / packages).

These generalize a node-outage postmortem into portable, side-effect-free
checks. Every case injects its probe callable so no real ``systemctl`` /
``nvidia-smi`` / ``dpkg`` is invoked — the checks stay hermetic.
"""

from __future__ import annotations

import axiom.cli.doctor as d
from axiom.cli.doctor import (
    CheckStatus,
    check_gpu_contention,
    check_managed_services,
    check_package_coherence,
    default_checks,
)

# ---- Managed services ------------------------------------------------------


def test_managed_services_flags_failed_unit(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Linux")
    monkeypatch.setattr(d.shutil, "which", lambda _n: "/usr/bin/systemctl")
    listing = (
        "neut-shim.service loaded active running Axiom shim\n"
        "neut-background-service.service loaded failed failed Axiom bg\n"
    )
    res = check_managed_services(lister=lambda: (0, listing))
    assert res.status is CheckStatus.ERROR
    assert "neut-background-service.service" in res.detail["failed"]
    # A doctor check never prescribes a blind mutating fix.
    assert "apt -f install" not in (res.fix_hint or "")


def test_managed_services_all_healthy(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Linux")
    monkeypatch.setattr(d.shutil, "which", lambda _n: "/usr/bin/systemctl")
    listing = "neut-shim.service loaded active running Axiom shim\n"
    res = check_managed_services(lister=lambda: (0, listing))
    assert res.status is CheckStatus.OK
    assert res.detail["failed"] == []


def test_managed_services_no_units_skips(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Linux")
    monkeypatch.setattr(d.shutil, "which", lambda _n: "/usr/bin/systemctl")
    res = check_managed_services(lister=lambda: (0, ""))
    assert res.status is CheckStatus.SKIPPED


def test_managed_services_off_systemd_skips(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Darwin")
    res = check_managed_services()
    assert res.status is CheckStatus.SKIPPED


# ---- GPU contention --------------------------------------------------------


def test_gpu_contention_warns_near_full(monkeypatch):
    monkeypatch.setattr(d.shutil, "which", lambda _n: "/usr/bin/nvidia-smi")
    res = check_gpu_contention(
        query=lambda: (0, "94000, 98000\n"),
        apps=lambda: (0, "88000, vllm\n5000, llama-server\n"),
    )
    assert res.status is CheckStatus.WARNING
    assert res.detail["used_fraction"] >= 0.9
    assert len(res.detail["compute_procs"]) == 2


def test_gpu_contention_ok_with_headroom(monkeypatch):
    monkeypatch.setattr(d.shutil, "which", lambda _n: "/usr/bin/nvidia-smi")
    res = check_gpu_contention(
        query=lambda: (0, "10000, 98000\n"),
        apps=lambda: (0, "10000, llama-server\n"),
    )
    assert res.status is CheckStatus.OK


def test_gpu_contention_no_gpu_skips(monkeypatch):
    monkeypatch.setattr(d.shutil, "which", lambda _n: None)
    res = check_gpu_contention()
    assert res.status is CheckStatus.SKIPPED


# ---- Package / repo coherence ---------------------------------------------


def test_package_coherence_flags_catch_all_pin(monkeypatch, tmp_path):
    monkeypatch.setattr(d.shutil, "which", lambda _n: "/usr/bin/dpkg")
    pin = tmp_path / "cuda-repository-pin-600"
    pin.write_text("Package: *\nPin: release l=NVIDIA CUDA\nPin-Priority: 600\n")
    res = check_package_coherence(auditor=lambda: (0, ""), preferences_dir=tmp_path)
    assert res.status is CheckStatus.WARNING
    assert res.detail["catch_all_pins"]
    # The fix_hint names apt -f install only to warn against running it blindly.
    hint = (res.fix_hint or "").lower()
    assert "apt -f install" in hint
    assert "not" in hint  # framed as the thing NOT to do


def test_package_coherence_flags_broken_dpkg(monkeypatch, tmp_path):
    monkeypatch.setattr(d.shutil, "which", lambda _n: "/usr/bin/dpkg")
    res = check_package_coherence(
        auditor=lambda: (0, "The following packages are in a mess:\n libfoo\n"),
        preferences_dir=tmp_path,  # empty dir -> no pins
    )
    assert res.status is CheckStatus.WARNING
    assert res.detail["broken_packages"]


def test_package_coherence_scoped_pin_is_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(d.shutil, "which", lambda _n: "/usr/bin/dpkg")
    # A scoped pin (not Package: *) must NOT trip the catch-all detector.
    pin = tmp_path / "cuda-repository-pin-600"
    pin.write_text("Package: cuda-toolkit*\nPin: release l=NVIDIA CUDA\nPin-Priority: 600\n")
    res = check_package_coherence(auditor=lambda: (0, ""), preferences_dir=tmp_path)
    assert res.status is CheckStatus.OK
    assert res.detail["catch_all_pins"] == []


def test_package_coherence_off_apt_skips(monkeypatch):
    monkeypatch.setattr(d.shutil, "which", lambda _n: None)
    res = check_package_coherence()
    assert res.status is CheckStatus.SKIPPED


# ---- Host-hazard enrichment (ADR-089) --------------------------------------


def _catch_all_pin(tmp_path):
    pin = tmp_path / "cuda-repository-pin-600"
    pin.write_text("Package: *\nPin: release l=Vendor\nPin-Priority: 600\n")


def test_package_coherence_enriched_with_host_hazard(monkeypatch, tmp_path):
    from axiom.infra.host_hazards import (
        SIG_APT_CATCH_ALL_PIN,
        HostHazard,
        StaticHostHazardProvider,
    )

    monkeypatch.setattr(d.shutil, "which", lambda _n: "/usr/bin/dpkg")
    _catch_all_pin(tmp_path)
    hz = HostHazard(
        host_id="node-7",
        signature=SIG_APT_CATCH_ALL_PIN,
        category="environment",
        consequence="fix-broken removes the running GPU driver 595",
        remediation="the tested fix is the scoped pin in ~/gpu-fix-backups/",
        recorded_at="2026-07-06",
    )
    prov = StaticHostHazardProvider("node-7", {SIG_APT_CATCH_ALL_PIN: hz})
    res = check_package_coherence(
        auditor=lambda: (0, ""), preferences_dir=tmp_path, hazards=prov
    )
    assert res.status is CheckStatus.WARNING
    # Generic warning escalated to the host-specific consequence + tested fix.
    assert "fix-broken removes the running GPU driver 595" in res.summary
    assert "gpu-fix-backups" in (res.fix_hint or "")
    assert res.detail["host_hazard"]["host_id"] == "node-7"
    assert res.detail["host_enrichment_available"] is True


def test_package_coherence_no_provider_reports_enrichment_unavailable(monkeypatch, tmp_path):
    # A catch-all pin with no registry: generic message, and enrichment is
    # explicitly flagged unavailable — never silently treated as "healthy".
    monkeypatch.setattr(d.shutil, "which", lambda _n: "/usr/bin/dpkg")
    _catch_all_pin(tmp_path)
    res = check_package_coherence(auditor=lambda: (0, ""), preferences_dir=tmp_path)
    assert res.status is CheckStatus.WARNING
    assert "host_hazard" not in res.detail
    assert res.detail["host_enrichment_available"] is False


def test_package_coherence_failsafe_when_provider_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(d.shutil, "which", lambda _n: "/usr/bin/dpkg")
    _catch_all_pin(tmp_path)

    class _Boom:
        host_id = "node-7"

        def active(self, signature):
            raise RuntimeError("registry down")

    res = check_package_coherence(
        auditor=lambda: (0, ""), preferences_dir=tmp_path, hazards=_Boom()
    )
    # Degrades to the generic warning; never crashes, never false-green.
    assert res.status is CheckStatus.WARNING
    assert res.detail["host_enrichment_available"] is False
    assert "apt -f install" in (res.fix_hint or "").lower()


# ---- Windows: managed scheduled tasks --------------------------------------


def _win_csv(*rows: str) -> str:
    header = '"HostName","TaskName","Status","Last Result","Scheduled Task State"'
    return "\n".join((header, *rows)) + "\n"


def test_managed_services_windows_flags_failed_task(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Windows")
    listing = _win_csv(
        '"WINBOX","\\Axiom_shim","Ready","0","Enabled"',
        '"WINBOX","\\Axiom_bg","Ready","1","Enabled"',
        '"WINBOX","\\UnrelatedVendorTask","Ready","2","Enabled"',  # not ours
    )
    res = check_managed_services(task_prefix="Axiom", lister=lambda: (0, listing))
    assert res.status is CheckStatus.ERROR
    assert "Axiom_bg" in res.detail["failed"]
    assert "Axiom_shim" not in res.detail["failed"]
    # A foreign task's failure must never be attributed to us.
    assert "UnrelatedVendorTask" not in res.detail["managed"]
    assert "apt -f install" not in (res.fix_hint or "")


def test_managed_services_windows_flags_disabled(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Windows")
    listing = _win_csv('"WINBOX","\\Axiom_shim","Ready","0","Disabled"')
    res = check_managed_services(task_prefix="Axiom", lister=lambda: (0, listing))
    assert res.status is CheckStatus.ERROR
    assert "Axiom_shim" in res.detail["failed"]


def test_managed_services_windows_all_healthy(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Windows")
    listing = _win_csv(
        '"WINBOX","\\Axiom_shim","Ready","0","Enabled"',
        '"WINBOX","\\Axiom_bg","Running","267009","Enabled"',  # running: benign
        '"WINBOX","\\Axiom_new","Ready","267011","Enabled"',   # never-run: benign
    )
    res = check_managed_services(task_prefix="Axiom", lister=lambda: (0, listing))
    assert res.status is CheckStatus.OK
    assert res.detail["failed"] == []
    assert len(res.detail["managed"]) == 3


def test_managed_services_windows_no_tasks_skips(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Windows")
    listing = _win_csv('"WINBOX","\\SomeoneElsesTask","Ready","0","Enabled"')
    res = check_managed_services(task_prefix="Axiom", lister=lambda: (0, listing))
    assert res.status is CheckStatus.SKIPPED


def test_managed_services_windows_schtasks_unavailable_skips(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Windows")
    res = check_managed_services(task_prefix="Axiom", lister=lambda: (1, ""))
    assert res.status is CheckStatus.SKIPPED


# ---- Windows: nvidia-smi discovery -----------------------------------------


def test_find_nvidia_smi_windows_system32(monkeypatch, tmp_path):
    # System32 is always on PATH in reality; here we prove the explicit
    # fallback so discovery works even when which() misses it.
    monkeypatch.setattr(d.platform, "system", lambda: "Windows")
    monkeypatch.setattr(d.shutil, "which", lambda _n: None)
    system32 = tmp_path / "Windows" / "System32"
    system32.mkdir(parents=True)
    exe = system32 / "nvidia-smi.exe"
    exe.write_text("")
    # Upper-case keys: real Windows env is case-insensitive; the POSIX test
    # host is not, and the code reads the upper-case form.
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path / "Windows"))
    assert d._find_nvidia_smi() == str(exe)


def test_find_nvidia_smi_absent_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(d.platform, "system", lambda: "Windows")
    monkeypatch.setattr(d.shutil, "which", lambda _n: None)
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path / "nowhere"))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "nowhere2"))
    assert d._find_nvidia_smi() is None


def test_gpu_contention_windows_uses_discovered_smi(monkeypatch):
    # When which() misses nvidia-smi but discovery finds it, the check must
    # still run rather than SKIP.
    monkeypatch.setattr(d, "_find_nvidia_smi", lambda: r"C:\Windows\System32\nvidia-smi.exe")
    res = check_gpu_contention(
        query=lambda: (0, "94000, 98000\n"),
        apps=lambda: (0, "88000, python.exe\n"),
    )
    assert res.status is CheckStatus.WARNING
    assert res.detail["used_fraction"] >= 0.9


# ---- Registration ----------------------------------------------------------


def test_new_checks_registered_in_default_set():
    names = {c.name for c in default_checks()}
    assert "Managed services healthy" in names
    assert "GPU memory contention" in names
    assert "Package/repo coherence" in names
