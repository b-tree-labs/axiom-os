# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit X1: missed checks and pending co-signatures in the oversight brief and
in Needs you.

Ticket: docs/working/logbook-units/X1.md in the product repo that holds the plan. A missed required
check and a co-signature that waited past its window become one brief item per log and claim kind,
in plain words, with no remedy (a person acts); the fleet stays in the brief once the attest source
is registered; a person's Needs you lists the drafts waiting for their signature and the entries
waiting for their co-signature, each linking to the Log page, with no action that signs.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import oversight, queue, registry, service  # noqa: E402
from axiom.extensions.builtins.attest import roles as role_store  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_obligations import (  # noqa: E402
    LOGBOOK,
    T0,
    clock,  # noqa: F401
    sign,
    watch,  # noqa: F401
)
from axiom.extensions.builtins.attest.tests.test_signing import SITE  # noqa: E402
from axiom.extensions.builtins.receipts import brief as brief_mod  # noqa: E402
from axiom.extensions.builtins.receipts import sources  # noqa: E402
from axiom.extensions.builtins.receipts.brief import NEEDS_YOU_CAP, OversightItem  # noqa: E402
from axiom.extensions.builtins.receipts.conditions import condition_for  # noqa: E402

from .test_l4 import entry, lead, member, pair_log  # noqa: E402,F401

# ruff: noqa: F811  (pytest fixtures imported from the obligation and L4 tests)

LEAD = "@op2:site-a"
MEMBER = "@op1:site-a"


@pytest.fixture(autouse=True)
def _fresh_sources():
    sources.reset_registry()
    yield
    sources.reset_registry()


def claims(kind: str, now):
    return [i for i in oversight.read_logbook_claims(sites=[SITE], now=now) if i.claim_kind == kind]


# -- A1: a missed required check ------------------------------------------------------------


@pytest.mark.acceptance("LB-X1-A1")
def test_lb_x1_a1_an_open_miss_is_one_failed_item_per_log_and_clears_when_signed(watch, node_signer):
    sign("START", node_signer)
    (item,) = claims("attest_missed_check", T0 + timedelta(minutes=31))
    assert (item.entity_kind, item.entity_id, item.status, item.site) == ("logbook", "watch_log", "failed", SITE)
    assert "CHECK was due" in item.evidence and "(1 open)" in item.evidence
    assert item.next_action is None
    words = condition_for("attest_missed_check", "failed")
    assert words.headline != condition_for("never_heard_of", "failed").headline
    assert words.fix and words.remedy is None
    watch["clock"]["t"] = T0 + timedelta(minutes=32)
    sign("CHECK", node_signer)
    assert claims("attest_missed_check", T0 + timedelta(minutes=33)) == []


@pytest.mark.acceptance("LB-X1-A1")
def test_lb_x1_a1_an_optional_obligations_miss_is_no_item(watch, node_signer):
    registry.register(parse_logbook({**LOGBOOK, "logbook": {"id": "watch_log", "version": "2"},
                                     "obligation": [{**LOGBOOK["obligation"][0], "required": False}]},
                                    source="test:watch"))  # fmt: skip
    sign("START", node_signer)
    assert claims("attest_missed_check", T0 + timedelta(minutes=31)) == []


# -- A2: a co-signature that waited too long ------------------------------------------------


@pytest.mark.acceptance("LB-X1-A2")
def test_lb_x1_a2_a_cosignature_past_its_window_is_stale_and_reserved_to_a_person(pair_log, node_signer):
    rec = entry(node_signer, "HANDOVER")
    from axiom.policy.action_ledger import parse_when

    signed = parse_when(rec["occurred_at"])
    assert claims("attest_pending_cosign", signed + timedelta(minutes=10)) == []  # inside its 30 m
    (item,) = claims("attest_pending_cosign", signed + timedelta(minutes=45))
    assert (item.entity_id, item.status) == ("pair_log", "stale")
    assert "1 entry has waited 45 min for a co-signature" in item.evidence
    words = condition_for("attest_pending_cosign", "stale")
    assert words.authority_reserved and words.remedy is None


@pytest.mark.acceptance("LB-X1-A2")
def test_lb_x1_a2_the_window_is_declared_and_defaults_to_an_hour(pair_log):
    assert registry.get("pair_log").type("HANDOVER").cosign.window_seconds == 30 * 60
    assert registry.get("pair_log").type("CHECK").cosign.window_seconds is None
    assert oversight.DEFAULT_COSIGN_WINDOW_SECONDS == 60 * 60
    assert "HANDOVER.cosign.window" not in registry.get("pair_log").not_yet_enforced


# -- A3: registration keeps the fleet --------------------------------------------------------


def _fleet(monkeypatch):
    node = OversightItem(entity_kind="node", entity_id="node-1", claim_kind="heartbeat",
                         status="stale", evidence="quiet", site=SITE)  # fmt: skip
    monkeypatch.setattr(brief_mod, "fleet_source", lambda session, **kw: [node])
    return node


@pytest.mark.acceptance("LB-X1-A3")
def test_lb_x1_a3_the_fleet_stays_once_the_attest_source_is_registered(watch, node_signer, monkeypatch):
    node = _fleet(monkeypatch)
    sign("START", node_signer)
    sources.bind_extension_sources()
    sources.bind_extension_sources()  # twice: no raise, no second reader
    names = sources.registry().names()
    assert names[0] == "fleet" and "attest_watch_log" in names
    assert len(names) == len(set(names))
    got = sources.all_claims(None, sites=[SITE], now=T0 + timedelta(minutes=31))
    assert got.items[0] == node, "the fleet keeps its place"
    assert any(i.claim_kind == "attest_missed_check" for i in got.items)


@pytest.mark.acceptance("LB-X1-A3")
def test_lb_x1_a3_a_failing_attest_source_is_named_not_swallowed(watch, monkeypatch):
    _fleet(monkeypatch)

    def broken(**_kw):
        raise ConnectionError("the attest store is unreachable")

    monkeypatch.setattr(oversight, "read_logbook", lambda logbook_id, **kw: broken())
    got = sources.all_claims(None, sites=[SITE], now=T0)
    assert ("attest_watch_log", "the log watch_log", "ConnectionError") in got.unreadable
    assert [i.entity_id for i in got.items] == ["node-1"]


# -- A4: the budget and the order ------------------------------------------------------------


@pytest.mark.acceptance("LB-X1-A4")
def test_lb_x1_a4_new_kinds_fall_after_the_known_ones_and_the_cap_holds():
    def it(kind, status):
        return OversightItem(entity_kind="x", entity_id=kind, claim_kind=kind, status=status, evidence="")

    mixed = [it("attest_pending_cosign", "stale"), it("heartbeat", "stale"), it("attest_missed_check", "failed"),
             it("backup", "failed")]  # fmt: skip
    ranked = [i.claim_kind for i in sorted(mixed, key=brief_mod._rank)]
    assert ranked == ["backup", "attest_missed_check", "heartbeat", "attest_pending_cosign"]
    assert NEEDS_YOU_CAP == 3
    assert brief_mod._KIND_PRIORITY == ["backup", "canary", "heartbeat", "service_health", "versions"]


# -- A5, A6: Needs you -----------------------------------------------------------------------


class _NoInbox:
    def query(self, _q):
        return []


def _queue(person, tmp_path):
    from axiom.extensions.builtins.notifications.needs_you import needs_you

    return needs_you(person, inbox_store=_NoInbox(), approvals_path=tmp_path / "none.json",
                     due_provider=lambda p, d: [])  # fmt: skip


@pytest.fixture
def people(pair_log, tmp_path, monkeypatch):
    role_store.grant(MEMBER, SITE, "member", by="@admin:site-a", state_dir=tmp_path)
    role_store.grant(LEAD, SITE, "lead", by="@admin:site-a", state_dir=tmp_path)
    monkeypatch.setattr(queue, "_state_dir", lambda: tmp_path)
    return tmp_path


@pytest.mark.acceptance("LB-X1-A5")
def test_lb_x1_a5_needs_you_lists_to_sign_and_co_sign_for_that_person_only(people, node_signer, tmp_path):
    rec = entry(node_signer, "HANDOVER", who=member())
    draft = service.create_draft(
        site_id=SITE, logbook="pair_log", entry_type="NOTE", meaning="authored",
        content={"title": "Note", "fields": {"text": "hello"}}, origin="agent:@helper:example",
        for_principal=LEAD,
    )  # fmt: skip
    q = _queue(LEAD, tmp_path)
    by = {s["id"]: s for s in q["sections"]}
    assert [s["id"] for s in q["sections"]][-2:] == ["sign", "cosign"]
    (cos,) = by["cosign"]["items"]
    assert cos["source"] == "attest" and cos["action"] is None
    assert cos["href"] == f"#/log?logbook=pair_log&record={rec['attestation_id']}"
    (to_sign,) = by["sign"]["items"]
    assert to_sign["href"] == f"#/log?logbook=pair_log&draft={draft}"
    assert q["count"] == len(q["items"])
    # The member signed the entry and holds no co-signing role: nothing for them, and the lead's
    # draft is never in their queue.
    mine = {s["id"]: s for s in _queue(MEMBER, tmp_path)["sections"]}
    assert mine["cosign"]["items"] == [] and mine["sign"]["items"] == []


@pytest.mark.acceptance("LB-X1-A5")
def test_lb_x1_a5_a_person_never_cosigns_their_own_entry(people, node_signer, tmp_path):
    role_store.grant(LEAD, SITE, "member", by="@admin:site-a", state_dir=tmp_path)
    entry(node_signer, "HANDOVER", who=member(handle=LEAD, roles=("member",)))
    assert {s["id"]: s for s in _queue(LEAD, tmp_path)["sections"]}["cosign"]["items"] == []


@pytest.mark.acceptance("LB-X1-A5")
def test_lb_x1_a5_a_failing_attest_queue_never_empties_needs_you(people, monkeypatch, tmp_path):
    def broken(*_a, **_k):
        raise ConnectionError("down")

    monkeypatch.setattr(queue, "for_person", broken)
    q = _queue(LEAD, tmp_path)
    assert q["count"] == 0 and [s["id"] for s in q["sections"]] == ["due", "held", "sign", "cosign"]


@pytest.mark.acceptance("LB-X1-A6")
def test_lb_x1_a6_nothing_here_signs():
    import inspect

    for mod in (oversight, queue):
        src = inspect.getsource(mod)
        assert "service.sign(" not in src and "create_cosign_draft" not in src
    assert Path(oversight.__file__).name == "oversight.py"
