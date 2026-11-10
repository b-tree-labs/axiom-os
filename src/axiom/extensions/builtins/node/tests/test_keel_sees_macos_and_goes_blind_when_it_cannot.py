# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A sensor that measured nothing may not report health (ADR-237 decision 3).

The transcripts are one developer Mac's real ``df -P -k``, ``mount`` and ``vm_stat``.
BSD ``df`` rejects GNU's ``-B1``; before this, KEEL read nothing on macOS and published
``even keel``, which admission control reads as room.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.node import collect
from axiom.extensions.builtins.node.collect import Collectors
from axiom.extensions.builtins.node.skills import vitals
from axiom.infra.chain_ledger import VITALS_FILE
from axiom.infra.skills import SkillContext

MAC_DF = """\
Filesystem     1024-blocks      Used Available Capacity  Mounted on
/dev/disk3s1s1   971298980  12347496 148561284     8%    /
devfs                  221       221         0   100%    /dev
/dev/disk3s6     971298980  18874820 148561284    12%    /System/Volumes/VM
/dev/disk3s2     971298980   7882204 148561284     6%    /System/Volumes/Preboot
/dev/disk1s2        563200      6164    543600     2%    /System/Volumes/xarts
/dev/disk3s5     971298980 781817564 148561284    85%    /System/Volumes/Data
map auto_home            0         0         0   100%    /System/Volumes/Data/home
/dev/disk9s2       1931836   1930428      1408   100%    /private/var/folders/zz/T/5EB2C7C8
/dev/disk12s1       512000    511000      1000   100%    /Volumes/Some Installer
/dev/disk13s1   1953125000 976562500 976562500    50%    /Volumes/Backup Disk
"""

MAC_MOUNT = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse, protect, root data)
/dev/disk9s2 on /private/var/folders/zz/T/5EB2C7C8 (hfs, local, nodev, nosuid, read-only, noowners, nobrowse)
/dev/disk12s1 on /Volumes/Some Installer (hfs, local, read-only, noowners)
/dev/disk13s1 on /Volumes/Backup Disk (apfs, local, journaled)
"""

MAC_VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    15624.
Pages active:                                 225441.
Pages inactive:                               206141.
Pages speculative:                             18259.
Pages throttled:                                   0.
Pages wired down:                             409629.
Pages purgeable:                                   4.
"""

MEMSIZE = "25769803776\n"
GOOD_MEM = {"MemTotal": 32 * 2**30, "MemAvailable": 16 * 2**30}
GOOD_DF = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 1000 100 900 10% /\n"


def _collectors(**over) -> Collectors:
    base = {"gpus": lambda: "", "mounts": lambda: "", "df": lambda: GOOD_DF}
    return Collectors(**{**base, **over})


def _gather(tmp_path: Path, *, meminfo=GOOD_MEM, **over):
    with collect.use(_collectors(**over)):
        return vitals.gather(SkillContext(registry=None, logger=logging.getLogger("t"), state_dir=tmp_path), meminfo=meminfo, load=(1.0, 1.0, 1.0))


def _run(tmp_path: Path, **over):
    with collect.use(_collectors(**over)):
        return vitals.run({}, SkillContext(registry=None, logger=logging.getLogger("t"), state_dir=tmp_path))


def test_the_portable_df_flag_is_the_one_the_collector_runs():
    """``-k`` is POSIX and BSD and GNU both accept it; ``-B1`` is GNU only."""
    calls: list[list[str]] = []
    real_run = collect.run
    try:
        collect.run = lambda cmd, timeout=10.0: calls.append(cmd) or ""  # type: ignore[assignment]
        Collectors().df()
    finally:
        collect.run = real_run  # type: ignore[assignment]
    assert calls == [["df", "-P", "-k"]]


def test_a_mac_reads_real_disks_in_bytes_from_block_counts():
    disks = collect.parse_df(MAC_DF, readonly=collect.parse_readonly_mounts(MAC_MOUNT))
    by_mount = {d["mount"]: d for d in disks}
    assert list(by_mount) == ["/", "/Volumes/Backup Disk"]
    assert by_mount["/"]["free_bytes"] == 148561284 * 1024
    assert by_mount["/"]["size_bytes"] == 971298980 * 1024


def test_a_shared_apfs_container_is_judged_on_what_can_still_be_written():
    """``/`` holds 1% of its container and 15% of the container is free. Judged on used/size
    this mount looked 99% free however full the machine was."""
    disks = collect.parse_df(MAC_DF, readonly=collect.parse_readonly_mounts(MAC_MOUNT))
    assert disks[0]["mount"] == "/" and disks[0]["used_pct"] == pytest.approx(84.7, abs=0.1)
    tight = (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/disk3s1s1 971298980 12347496 9712989 56% /\n"
    )
    judged = vitals.judge_disks(collect.parse_df(tight), {})
    assert judged[0]["state"] == "critical"  # 1% of the container is free


def test_a_full_read_only_image_is_not_a_disk_emergency():
    """A mounted installer sits at 100% for its whole life; nothing can be written there."""
    disks = collect.parse_df(MAC_DF, readonly=collect.parse_readonly_mounts(MAC_MOUNT))
    assert not [d for d in disks if d["mount"].startswith(("/private/var/folders", "/Volumes/Some"))]


def test_the_sealed_root_stays_even_though_it_mounts_read_only():
    assert "/" in collect.parse_readonly_mounts(MAC_MOUNT)
    assert [d["mount"] for d in collect.parse_df(MAC_DF, readonly={"/"})][0] == "/"


def test_linux_mount_output_marks_read_only_the_same_way():
    text = (
        "/dev/sda1 on / type ext4 (rw,relatime)\n"
        "/dev/loop3 on /snap/core/1 type squashfs (ro,nodev)\n"
    )
    assert collect.parse_readonly_mounts(text) == {"/snap/core/1"}


def test_a_gnu_df_with_one_byte_blocks_still_reads():
    out = collect.parse_df(
        "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/sda1 1000 100 900 10% /\n"
    )
    assert out[0]["free_bytes"] == 900


def test_memory_on_a_mac_comes_from_vm_stat_and_sysctl():
    mem = collect.parse_vm_stat(MAC_VM_STAT, MEMSIZE)
    assert mem["MemTotal"] == 25769803776
    assert mem["MemAvailable"] == (15624 + 206141 + 18259 + 4) * 16384


def test_unreadable_memory_tools_read_as_nothing():
    assert collect.parse_vm_stat("", "") == {}
    assert collect.parse_vm_stat(MAC_VM_STAT, "") == {}


def test_a_mac_gets_a_real_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(vitals, "_meminfo", lambda: {})  # no /proc on a Mac
    r = _gather(
        tmp_path,
        meminfo=None,
        df=lambda: MAC_DF,
        mounts=lambda: MAC_MOUNT,
        vm_stat=lambda: MAC_VM_STAT,
        memsize=lambda: MEMSIZE,
    )
    assert r["verdict"] == "even keel" and r["blind"] == []
    assert r["memory"]["total_bytes"] == 25769803776
    assert [d["mount"] for d in r["disks"]] == ["/", "/Volumes/Backup Disk"]


def test_no_disk_reading_is_blind_never_a_clean_verdict(tmp_path):
    r = _gather(tmp_path, df=lambda: "")
    assert r["verdict"] == "blind" and r["blind"] == ["disk"] and r["disks"] == []
    assert any(f["code"] == "disk-blind" for f in r["findings"])


def test_no_memory_reading_is_blind_too(tmp_path):
    r = _gather(tmp_path, meminfo={})
    assert r["verdict"] == "blind" and r["blind"] == ["memory"]


def test_a_measured_problem_outranks_blindness(tmp_path):
    full = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 1000 990 10 99% /\n"
    r = _gather(tmp_path, meminfo={}, df=lambda: full)
    assert r["verdict"] == "critical" and r["blind"] == ["memory"]


def test_blind_is_published_for_admission_and_says_what_it_could_not_read(tmp_path):
    r = _run(tmp_path, df=lambda: "")
    doc = json.loads((tmp_path / VITALS_FILE).read_text())
    assert doc["verdict"] == "blind" and "disk" in doc["blind"]
    assert r.ok and r.value["verdict"] == "blind"


def test_blind_points_at_the_doctor_not_at_reclaiming_space(tmp_path):
    r = _run(tmp_path, df=lambda: "")
    assert [s["target"] for s in r.next] == ["node.doctor"]
