# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Erasing under storage pressure (ADR-290 D8, the owner's rule of 2026-11-25).

Only when the log store is over its declared budget does the schedule erase, and only content past its
declared ``delete_after_days`` and outside any hold: first the periods already tombstoned, oldest first,
then a period that was never archived, whose tombstone says so. It stops as soon as the pressure clears.
Content inside its rule is never erased; the schedule reports that the store is still over budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("fastapi")

from axiom.attest import verify as verify_lib  # noqa: E402
from axiom.extensions.builtins.attest import registry, service  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import SITE  # noqa: E402

from .test_plain import decl, write  # noqa: E402
from .test_plain import logs as _logs  # noqa: E402

logs = _logs  # the plain-log fixture, by the name its tests use

LOG = "pressured_notes"
SEAL = '[seal]\nby_roles = ["lead"]\nboundary = "range"\n'
RETENTION = 'retention = { delete_after_days = 2000, tombstone_by = ["lead"] }\n'  # no live_days: never archived
DRIVER = "@scheduler:site-a"
MIB = 2**20


@pytest.fixture(autouse=True)
def no_site_config(monkeypatch):
    monkeypatch.delenv("AXIOM_AGENT_ACCOUNTABLE", raising=False)
    monkeypatch.delenv("AXIOM_LOG_BUDGET_BYTES", raising=False)


@pytest.fixture
def two_periods(logs, tmp_path, monkeypatch):
    """A plain log with a deletion rule and two sealed periods, never archived."""
    import axiom.infra.paths as paths

    monkeypatch.setattr(paths, "get_user_state_dir", lambda: tmp_path)
    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(tmp_path / "changes.jsonl"))
    registry.register(
        parse_logbook(
            decl(LOG, extra_head=RETENTION, extra=SEAL, signed=False), source="pressure-test"
        )
    )
    seals = []
    for lo in (1, 4):
        for i in range(3):
            write(log=LOG, text=f"entry {lo + i}")
        seals.append(
            service.record_seal(
                site_id=SITE,
                logbook=LOG,
                summary=f"from {lo}",
                actor="@lee:site-a",
                actor_kind="human",
                roles=("lead",),
                boundary="range",
                from_seq=lo if lo == 1 else 5,
                to_seq=3 if lo == 1 else 7,
            )  # fmt: skip
        )
    return {"seals": seals, "signer": logs}


def at(monkeypatch, days):
    from axiom.extensions.builtins.attest import retention

    monkeypatch.setattr(retention, "_now", lambda: datetime.now(UTC) + timedelta(days=days))


def store_holds(monkeypatch, sizes):
    """The log store's size, one reading per call (the last repeats): the pressure as erasing frees it."""
    from axiom.extensions.builtins.attest import archival

    seq = list(sizes)
    monkeypatch.setattr(archival, "log_store_bytes", lambda: seq.pop(0) if len(seq) > 1 else seq[0])


def relieve(**kw):
    from axiom.extensions.builtins.attest import archival

    return archival.relieve_pressure(actor=DRIVER, **kw)


def statuses():
    from axiom.extensions.builtins.attest import retention

    return [p["tombstone_status"] for p in retention.report(SITE, LOG)["periods"]]


@pytest.mark.acceptance("LB-AP-A1")
def test_ap_a1_with_no_budget_declared_nothing_is_ever_under_pressure(two_periods, monkeypatch):
    at(monkeypatch, 2100)
    store_holds(monkeypatch, [10**12])
    out = relieve()
    assert out["pressure"] is None and out["acts"] == []
    assert statuses() == [None, None]


@pytest.mark.acceptance("LB-AP-A2")
def test_ap_a2_under_budget_nothing_is_erased_however_old(two_periods, monkeypatch):
    monkeypatch.setenv("AXIOM_LOG_BUDGET_BYTES", str(100 * MIB))
    at(monkeypatch, 2100)
    store_holds(monkeypatch, [50 * MIB])
    assert relieve()["acts"] == []
    assert statuses() == [None, None]


@pytest.mark.acceptance("LB-AP-A3")
def test_ap_a3_over_budget_a_never_archived_period_past_its_rule_is_erased_oldest_first_until_clear(
    two_periods, monkeypatch
):
    monkeypatch.setenv("AXIOM_LOG_BUDGET_BYTES", str(100 * MIB))
    at(monkeypatch, 2100)
    store_holds(monkeypatch, [150 * MIB, 90 * MIB])  # erasing the first period clears it
    out = relieve()
    assert [a["act"] for a in out["acts"]] == ["erased"], out
    assert out["acts"][0]["seal_id"] == two_periods["seals"][0]["attestation_id"]
    assert statuses() == ["erased", None]
    stone = next(r for r in service.records(SITE, LOG) if r["kind"] == "tombstone")
    assert stone["content"]["tombstone"]["archive"] == {"archive_id": None, "reason": "no archive: erased "
                                                        "under storage pressure past the declared rule"}  # fmt: skip
    assert stone["signer"]["performed_by"] == DRIVER
    keys = {two_periods["signer"].key_id: two_periods["signer"].public_bytes}
    report = verify_lib.chain(SITE, LOG, 1, len(service.records(SITE, LOG)), public_keys=keys)
    assert report.ok, report


@pytest.mark.acceptance("LB-AP-A4")
def test_ap_a4_a_period_already_tombstoned_is_erased_before_anything_new_is_tombstoned(
    two_periods, monkeypatch
):
    from axiom.extensions.builtins.attest import archival

    monkeypatch.setenv("AXIOM_LOG_BUDGET_BYTES", str(100 * MIB))
    at(monkeypatch, 2100)
    second = two_periods["seals"][1]["attestation_id"]
    archival.tombstone_unarchived(
        SITE, LOG, second, actor=DRIVER
    )  # tombstoned and hidden, not erased
    store_holds(monkeypatch, [150 * MIB, 90 * MIB])
    out = relieve()
    assert [(a["act"], a["seal_id"]) for a in out["acts"]] == [("erased", second)]
    assert statuses() == [None, "erased"]


@pytest.mark.acceptance("LB-AP-A5")
def test_ap_a5_content_inside_its_rule_is_never_erased_and_the_pressure_is_reported(
    two_periods, monkeypatch
):
    monkeypatch.setenv("AXIOM_LOG_BUDGET_BYTES", str(100 * MIB))
    at(monkeypatch, 10)
    store_holds(monkeypatch, [150 * MIB])
    out = relieve()
    assert out["acts"] == []
    assert "over their budget" in out["pressure"] and out["still_over"]
    assert statuses() == [None, None]


@pytest.mark.acceptance("LB-AP-A6")
def test_ap_a6_a_held_log_is_never_erased_under_pressure(two_periods, monkeypatch):
    from axiom.extensions.builtins.attest import retention

    monkeypatch.setenv("AXIOM_LOG_BUDGET_BYTES", str(100 * MIB))
    monkeypatch.setattr(retention, "held", lambda site, book: "a legal hold")
    at(monkeypatch, 2100)
    store_holds(monkeypatch, [150 * MIB])
    out = relieve()
    assert all(a["act"] != "erased" for a in out["acts"]) and out["still_over"]
    assert statuses() == [None, None]
