# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The scheduled archival driver (ADR-290 D8): routine archiving and tombstoning by the declared rule.

A sealed period whose ``live_days`` have passed is archived; one whose ``delete_after_days`` have passed
is tombstoned and its content hidden from reads, signed as the human the site names as accountable for
the scheduler, or by the scheduler itself when none is named. Erasing stays a named approver's act. Nothing
is done before it is due, under a hold, or twice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import registry, service  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import SITE  # noqa: E402

from .test_plain import decl, write  # noqa: E402
from .test_plain import logs as _logs  # noqa: E402

logs = _logs  # the plain-log fixture, by the name its tests use

ROUTINE = "routine_notes"
SEAL = '[seal]\nby_roles = ["lead"]\nboundary = "range"\n'
RETENTION = 'retention = { live_days = 30, delete_after_days = 2000, tombstone_by = ["lead"] }\n'
DRIVER = "@scheduler:site-a"


@pytest.fixture(autouse=True)
def no_site_config(monkeypatch):
    monkeypatch.delenv("AXIOM_AGENT_ACCOUNTABLE", raising=False)


@pytest.fixture
def sealed(logs, tmp_path, monkeypatch):
    """A plain log with a seal, an archiving rule and a deletion rule; three entries sealed by a person."""
    import axiom.infra.paths as paths
    from axiom.extensions.builtins.attest import archives
    from axiom.infra.storage import provider_from_config

    monkeypatch.setattr(paths, "get_user_state_dir", lambda: tmp_path)
    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(tmp_path / "changes.jsonl"))
    archives.set_target(provider_from_config({"base_dir": str(tmp_path / "cold")}))
    registry.register(parse_logbook(decl(ROUTINE, extra_head=RETENTION, extra=SEAL, signed=False),
                                    source="driver-test"))  # fmt: skip
    for i in range(1, 4):
        write(log=ROUTINE, text=f"entry {i}")
    seal = service.record_seal(site_id=SITE, logbook=ROUTINE, summary="the first three", actor="@lee:site-a",
                               actor_kind="human", roles=("lead",), boundary="range", from_seq=1, to_seq=3)  # fmt: skip
    yield {"seal": seal, "signer": logs}
    archives.set_target(None)
    archives._packages.clear()  # noqa: SLF001


def at(monkeypatch, days):
    from axiom.extensions.builtins.attest import retention

    monkeypatch.setattr(retention, "_now", lambda: datetime.now(UTC) + timedelta(days=days))


def run(**kw):
    from axiom.extensions.builtins.attest import archival

    return archival.run_due(SITE, ROUTINE, actor=DRIVER, actor_kind="machine", **kw)


@pytest.mark.acceptance("LB-AD-A1")
def test_ad_a1_nothing_is_done_before_it_is_due(sealed, monkeypatch):
    at(monkeypatch, 1)
    assert run() == []


@pytest.mark.acceptance("LB-AD-A2")
def test_ad_a2_a_period_past_live_days_is_archived_once(sealed, monkeypatch):
    from axiom.extensions.builtins.attest import retention

    at(monkeypatch, 31)
    acts = run()
    assert [a["act"] for a in acts] == ["archived"]
    period = retention.report(SITE, ROUTINE)["periods"][0]
    assert period["archive_status"] == "archived"
    assert run() == []  # done once; the next tick finds nothing due


@pytest.mark.acceptance("LB-AD-A3")
def test_ad_a3_a_period_past_delete_after_days_is_tombstoned_and_hidden_signed_as_the_named_human(
    sealed, monkeypatch
):
    from axiom.extensions.builtins.attest import retention

    monkeypatch.setenv("AXIOM_AGENT_ACCOUNTABLE", "*=@sam:site-a")
    at(monkeypatch, 2100)
    acts = run()
    assert [a["act"] for a in acts] == ["archived", "tombstoned"]
    stone = acts[1]["record"]
    assert stone["kind"] == "tombstone" and stone["target"] == sealed["seal"]["attestation_id"]
    assert (
        stone["signer"]["principal"] == "@sam:site-a" and stone["signer"]["kind"] == "accountable"
    )
    assert stone["signer"]["performed_by"] == DRIVER
    period = retention.report(SITE, ROUTINE)["periods"][0]
    assert period["tombstone_status"] == "committed"  # reads hide it; nothing is erased
    assert run() == []


@pytest.mark.acceptance("LB-AD-A4")
def test_ad_a4_with_no_human_named_the_scheduler_signs_as_itself(sealed, monkeypatch):
    at(monkeypatch, 2100)
    stone = run()[1]["record"]
    assert stone["signer"]["principal"] == DRIVER and stone["signer"]["kind"] == "actor"


@pytest.mark.acceptance("LB-AD-A5")
def test_ad_a5_a_held_log_is_archived_but_nothing_is_tombstoned(sealed, monkeypatch):
    from axiom.extensions.builtins.attest import retention

    monkeypatch.setattr(retention, "held", lambda site, book: "a legal hold")
    at(monkeypatch, 2100)
    acts = run()
    assert [a["act"] for a in acts] == ["archived", "refused"]
    assert "hold" in acts[1]["reason"]
    assert retention.report(SITE, ROUTINE)["periods"][0]["tombstone_status"] is None


@pytest.mark.acceptance("LB-AD-A6")
def test_ad_a6_the_scheduled_skill_drives_every_sealed_log_at_every_site(
    sealed, monkeypatch, tmp_path
):
    import logging

    from axiom.extensions.builtins.attest.skills import archival as skill

    at(monkeypatch, 31)
    ctx = type("Ctx", (), {"state_dir": tmp_path, "logger": logging.getLogger("test.ad")})()
    done = skill.tick({}, ctx)
    assert done.ok, done.errors
    assert any(a["logbook"] == ROUTINE and a["act"] == "archived" for a in done.value["acts"])
