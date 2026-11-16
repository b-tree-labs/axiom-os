# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A node records each boot and whether the run before it stopped cleanly.

ADR-182 D5a attributes an outage to power only on positive evidence: a fresh
boot with no clean stop before it. A clean OS shutdown stops services with
SIGTERM, which records a clean stop; a power loss records nothing. So the
previous run's last word is the evidence, and its absence is the signal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.infra import boot_evidence as be

BOOT_1 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
BOOT_2 = BOOT_1 + timedelta(days=2)


def test_the_first_start_records_its_boot_without_judging_it(tmp_path):
    p = tmp_path / "boots.jsonl"
    be.record_start(path=p, boot_time=BOOT_1)
    (b,) = be.boots(path=p)
    assert b["at"] == BOOT_1.isoformat()
    assert b["clean_shutdown"] is None  # nothing was running before to judge


def test_a_restart_within_one_boot_records_no_boot(tmp_path):
    p = tmp_path / "boots.jsonl"
    be.record_start(path=p, boot_time=BOOT_1)
    be.record_clean_stop(path=p)
    be.record_start(path=p, boot_time=BOOT_1)
    assert len(be.boots(path=p)) == 1


def test_a_boot_after_a_clean_stop_is_clean(tmp_path):
    p = tmp_path / "boots.jsonl"
    be.record_start(path=p, boot_time=BOOT_1)
    be.record_clean_stop(path=p)
    be.record_start(path=p, boot_time=BOOT_2)
    assert be.boots(path=p)[-1] == {"at": BOOT_2.isoformat(), "clean_shutdown": True}


def test_a_boot_with_no_clean_stop_before_it_is_not_clean(tmp_path):
    """Power loss: the process was running, then the machine came back."""
    p = tmp_path / "boots.jsonl"
    be.record_start(path=p, boot_time=BOOT_1)
    be.record_start(path=p, boot_time=BOOT_2)
    assert be.boots(path=p)[-1] == {"at": BOOT_2.isoformat(), "clean_shutdown": False}


def test_a_clean_stop_then_a_crash_restart_then_power_loss_is_not_clean(tmp_path):
    """Only the LAST word before the boot counts."""
    p = tmp_path / "boots.jsonl"
    be.record_start(path=p, boot_time=BOOT_1)
    be.record_clean_stop(path=p)
    be.record_start(path=p, boot_time=BOOT_1)  # restarted, same boot
    be.record_start(path=p, boot_time=BOOT_2)  # never stopped cleanly again
    assert be.boots(path=p)[-1]["clean_shutdown"] is False


def test_an_unknown_boot_time_records_nothing(tmp_path):
    """No evidence is better than invented evidence."""
    p = tmp_path / "boots.jsonl"
    be.record_start(path=p, boot_time=None)
    assert be.boots(path=p) == []


def test_this_machines_boot_time_is_read_or_honestly_unknown():
    t = be.boot_time()
    assert t is None or (t.tzinfo is not None and t < datetime.now(UTC))


def test_the_default_ledger_is_redirectable(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_BOOT_LEDGER", str(tmp_path / "b.jsonl"))
    be.record_start(boot_time=BOOT_1)
    assert (tmp_path / "b.jsonl").exists()
