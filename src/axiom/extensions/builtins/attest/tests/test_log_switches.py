# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Log's product switches (switches.py): shift logs and log acknowledgements, off by default.

- ``shift_logs`` off: a log that declares a site-clock period is omitted from every listing, a read
  of it is the not-found a missing log gets, and drafting, signing or sealing in it is refused in
  words; its stored records are kept.
- ``log_acknowledgements`` off: an acknowledgement is neither drafted nor signed, nothing asks
  anyone to acknowledge, and signed acknowledgements stay readable.
- The upgrade keeps acknowledgements on for a site that already used them, once, with an audit
  record; a fresh site stays off; an explicit override is never touched.
- Approvals have no switch: a fresh node's Needs you lists nothing approval-related.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from axiom.extensions.builtins.attest import queue, registry, service, switches
from axiom.extensions.builtins.attest.logbooks import LogbookError
from axiom.extensions.builtins.attest.skills import bind_default
from axiom.infra.skills import SkillContext

from .test_signing import SITE, _round_check, _sign

SHIFT = "shift_log"
ME = "@op1:site-a"
OTHER = "@op2:site-a"

_SHIFT_LOG = '''
[logbook]
id = "shift_log"
handle = "shift"
version = "1"
display = "Shift Log"
assurance = { posture = "sso" }

[[type]]
id = "NOTE"
meanings = ["authored"]
roles = ["operator"]
assurance = { posture = "attested" }
fields = [{ id = "text", type = "text", required = true }]

[period.day]
starts = ["06:00", "18:00"]

[seal]
by_roles = ["operator"]
'''


@pytest.fixture(autouse=True)
def _fresh_switches(monkeypatch):
    for key in ("SHIFT_LOGS", "LOG_ACKNOWLEDGEMENTS", "LOG_PAPER"):
        monkeypatch.delenv(f"AXIOM_FLAG_{key}", raising=False)
    switches.reset()
    yield
    switches.reset()


@pytest.fixture
def shift_log(attest_db, tmp_path):
    path = tmp_path / "shift_log.toml"
    path.write_text(_SHIFT_LOG)
    registry.register_file(path)
    return SHIFT


def _note(text="hand-over"):
    return service.create_draft(site_id=SITE, logbook=SHIFT, entry_type="NOTE", meaning="authored",
                                content={"title": "Note", "fields": {"text": text}}, origin="human",
                                for_principal=ME)  # fmt: skip


def _on(monkeypatch, key):
    monkeypatch.setenv(f"AXIOM_FLAG_{key.upper()}", "on")


def _ctx(tmp_path) -> SkillContext:
    import logging

    from axiom.infra.principal import PrincipalContext

    return SkillContext(registry=bind_default(), state_dir=tmp_path, logger=logging.getLogger("test.switches"),
                        principal=PrincipalContext(handle=ME, posture="sso", assured=True))


# -- the declarations --------------------------------------------------------------------


def test_the_three_switches_are_declared_off_permanent_and_named_for_settings():
    from axiom.extensions.builtins.features.flag_registry import parse_manifest_flags

    manifest = tomllib.loads((Path(switches.__file__).parent / "axiom-extension.toml").read_text())
    flags = {f.key: f for f in parse_manifest_flags(manifest, owner="attest")}
    starts = {"shift_logs": "Shift logs", "log_paper": "Paper view", "log_acknowledgements": "Log acknowledgements"}
    for key, name in starts.items():
        f = flags[key]
        assert f.default == "off" and f.description.startswith(name)
        assert f.kind == "permission" and f.remove_by is None and f.stale_exempt, "a permanent switch"


def test_a_shift_log_is_one_that_declares_a_site_clock_period(attest_db, shift_log):
    assert switches.is_shift_log(registry._logbooks[SHIFT])
    assert not switches.is_shift_log(registry._logbooks["demo_log"])


# -- shift logs ----------------------------------------------------------------------------


def test_shift_logs_off_the_log_is_not_listed_and_reads_as_missing(shift_log):
    assert [b.id for b in registry.all_logbooks()] == ["demo_log"]
    with pytest.raises(LogbookError) as off:
        registry.get(SHIFT)
    with pytest.raises(LogbookError) as missing:
        registry.get("no_such_log")
    assert str(off.value) == str(missing.value).replace("no_such_log", SHIFT)
    assert registry.by_handle("shift") is None


def test_shift_logs_on_the_log_is_listed(shift_log, monkeypatch):
    _on(monkeypatch, "shift_logs")
    assert [b.id for b in registry.all_logbooks()] == ["demo_log", SHIFT]
    assert registry.get(SHIFT).id == SHIFT and registry.by_handle("shift").id == SHIFT


def test_shift_logs_off_drafting_signing_and_sealing_are_refused_in_words(shift_log, monkeypatch, node_signer):
    _on(monkeypatch, "shift_logs")
    waiting = _note()
    monkeypatch.delenv("AXIOM_FLAG_SHIFT_LOGS")
    with pytest.raises(service.AttestRefused, match="Shift logs are off on this site; an admin can turn"):
        _note()
    with pytest.raises(service.AttestRefused, match="Shift logs are off"):
        service.present(waiting)
    with pytest.raises(service.AttestRefused, match="Shift logs are off"):
        service.create_seal_draft(site_id=SITE, logbook=SHIFT, summary="all quiet", origin="human",
                                  for_principal=ME)  # fmt: skip


def test_shift_logs_off_the_records_are_kept_and_hidden_then_back_when_on(shift_log, monkeypatch, node_signer):
    from .test_signing import person

    _on(monkeypatch, "shift_logs")
    rec = _sign(_note(), node_signer, who=person(posture="sso")).record
    waiting = _note("second")
    monkeypatch.delenv("AXIOM_FLAG_SHIFT_LOGS")
    assert service.record(rec["attestation_id"]) is not None, "stored records are kept"
    assert [d["draft_id"] for d in service.list_drafts(for_principal=ME)] == []
    assert queue.for_person(ME, SITE) == []
    _on(monkeypatch, "shift_logs")
    assert [d["draft_id"] for d in service.list_drafts(for_principal=ME)] == [waiting]


def test_shift_logs_off_the_skills_answer_as_for_a_missing_log_and_refuse_writes(shift_log, tmp_path):
    c = _ctx(tmp_path)
    listed = c.registry.invoke("attest.logbook_list", {}, c)
    assert listed.ok and [b["id"] for b in listed.value["logbooks"]] == ["demo_log"]
    shown = c.registry.invoke("attest.show", {"site": SITE, "logbook": "shift"}, c)
    assert not shown.ok and shown.errors == ["no logbook 'shift_log' is declared on this node"]
    drafted = c.registry.invoke("attest.propose", {"site": SITE, "logbook": SHIFT, "type": "NOTE",
                                                   "meaning": "authored", "title": "x", "for_principal": ME}, c)  # fmt: skip
    assert not drafted.ok and drafted.errors == [switches.SHIFT_LOGS_OFF]


def test_shift_logs_off_the_http_routes_list_nothing_and_answer_not_found(shift_log, monkeypatch, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.attest import api
    from axiom.webauth.keys import reset_key_store_for_tests

    from .test_api import BASE, OP1, as_

    reset_key_store_for_tests()
    monkeypatch.setattr(api, "_state_dir", lambda: tmp_path)
    app = fastapi.FastAPI()
    router = fastapi.APIRouter(prefix="/api/v1")
    api.register_routes(router)
    app.include_router(router)
    c = as_(TestClient(app, base_url=BASE), OP1)
    assert [b["id"] for b in c.get("/api/v1/attest/logbooks").json()["logbooks"]] == ["demo_log"]
    off, missing = c.get(f"/api/v1/attest/logbooks/{SHIFT}"), c.get("/api/v1/attest/logbooks/nope")
    assert off.status_code == missing.status_code == 404
    assert off.json()["code"] == missing.json()["code"] == "not_found"
    assert c.get(f"/api/v1/attest/{SHIFT}/records").status_code == 404
    _on(monkeypatch, "shift_logs")
    assert SHIFT in [b["id"] for b in c.get("/api/v1/attest/logbooks").json()["logbooks"]]
    reset_key_store_for_tests()


# -- acknowledgements ------------------------------------------------------------------------


def test_acks_off_an_acknowledgement_is_neither_drafted_nor_signed(attest_db, monkeypatch, node_signer):
    from .test_signing import person

    rec = _sign(_round_check(), node_signer).record
    with pytest.raises(service.AttestRefused, match="Log acknowledgements are off on this site"):
        service.create_ack_draft(site_id=SITE, target=rec["attestation_id"], origin="human", for_principal=OTHER)
    _on(monkeypatch, "log_acknowledgements")
    d = service.create_ack_draft(site_id=SITE, target=rec["attestation_id"], origin="human", for_principal=OTHER)
    pres = service.present(d)
    monkeypatch.delenv("AXIOM_FLAG_LOG_ACKNOWLEDGEMENTS")
    with pytest.raises(service.AttestRefused, match="Log acknowledgements are off"):
        service.sign(pres.presentation_id, signatory=person(handle=OTHER), signer=node_signer)


def test_acks_off_nothing_asks_anyone_to_acknowledge_and_signed_ones_stay_readable(attest_db, monkeypatch,
                                                                                    node_signer):  # fmt: skip
    from .test_signing import person

    rec = _sign(_round_check(), node_signer).record
    _on(monkeypatch, "log_acknowledgements")
    ack = _sign(service.create_ack_draft(site_id=SITE, target=rec["attestation_id"], origin="human",
                                         for_principal=OTHER),
                node_signer, who=person(handle=OTHER)).record  # fmt: skip
    rec2 = _sign(_round_check(reading="later"), node_signer).record
    asked = service.create_ack_draft(site_id=SITE, target=rec2["attestation_id"], origin="human",
                                     for_principal=OTHER)  # fmt: skip
    assert [r["attestation_id"] for r in service.unacknowledged(SITE, "demo_log")] == [rec2["attestation_id"]]
    assert [d["draft_id"] for d in service.list_drafts(for_principal=OTHER)] == [asked]
    monkeypatch.delenv("AXIOM_FLAG_LOG_ACKNOWLEDGEMENTS")
    assert service.unacknowledged(SITE, "demo_log") == []
    assert service.list_drafts(for_principal=OTHER) == []
    assert [i for i in queue.for_person(OTHER, SITE) if i["section"] == "sign"] == []
    assert service.record(ack["attestation_id"])["kind"] == "ack", "a signed acknowledgement stays readable"


# -- the upgrade ---------------------------------------------------------------------------


class _Store:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def rows(self, key=None):
        return [r for r in self._rows if key is None or r["key"] == key]

    def overrides_for(self, scope, scope_id):
        return {r["key"]: r["value"] for r in self._rows if (r["scope"], r["scope_id"]) == (scope, scope_id)}

    def entries_for(self, scope, scope_id):
        return {r["key"]: r for r in self._rows if (r["scope"], r["scope_id"]) == (scope, scope_id)}

    def put(self, scope, scope_id, key, value, *, by, reason=None, **kw):
        row = {"scope": scope, "scope_id": scope_id, "key": key, "value": value, "updated_by": by,
               "reason": reason}  # fmt: skip
        self._rows = [r for r in self._rows if (r["scope"], r["scope_id"], r["key"]) != (scope, scope_id, key)]
        self._rows.append(row)
        return row

    def delete(self, scope, scope_id, key):
        return False


def _svc(store):
    from axiom.extensions.builtins.features.flag_registry import FlagDef
    from axiom.extensions.builtins.features.resolver import FlagService

    reg = {switches.LOG_ACKNOWLEDGEMENTS: FlagDef(key=switches.LOG_ACKNOWLEDGEMENTS, owner="attest")}
    return FlagService(store, registry=reg, variant=None)


def test_upgrade_a_site_with_acknowledgement_history_keeps_them_on_with_an_audit_record(tmp_path):
    from axiom.infra.audit_trail import audit_path, read_chain

    store = _Store()
    row = switches.keep_acks_on_upgrade(service=_svc(store), has_history=lambda: True, node="n1",
                                        state_dir=tmp_path)  # fmt: skip
    assert row["value"] == "on" and row["reason"] == switches.KEPT_ON_REASON
    assert store.rows(switches.LOG_ACKNOWLEDGEMENTS) == [row] and row["scope"] == "node" and row["scope_id"] == "n1"
    assert _svc(store).resolve_flag(switches.LOG_ACKNOWLEDGEMENTS, node="n1") == "on"
    (audit,) = [r for r in read_chain(audit_path(tmp_path)) if r.get("skill") == "flags.set"]
    assert audit["change"] == {"kind": "flag", "subject": "log_acknowledgements (node n1)", "old": None, "new": "on"}
    # Idempotent: the override now exists, so a second run writes nothing.
    assert switches.keep_acks_on_upgrade(service=_svc(store), has_history=lambda: True, node="n1",
                                         state_dir=tmp_path) is None  # fmt: skip
    assert len(store.rows()) == 1


def test_upgrade_a_fresh_site_stays_off(tmp_path):
    store = _Store()
    assert switches.keep_acks_on_upgrade(service=_svc(store), has_history=lambda: False, node="n1",
                                         state_dir=tmp_path) is None  # fmt: skip
    assert store.rows() == []
    assert _svc(store).resolve_flag(switches.LOG_ACKNOWLEDGEMENTS, node="n1") == "off"


def test_upgrade_never_touches_an_explicit_override(tmp_path):
    explicit = {"scope": "account", "scope_id": "site-a", "key": switches.LOG_ACKNOWLEDGEMENTS, "value": "off",
                "updated_by": "@admin:site-a", "reason": "we do not use them"}  # fmt: skip
    store = _Store([explicit])
    assert switches.keep_acks_on_upgrade(service=_svc(store), has_history=lambda: True, node="n1",
                                         state_dir=tmp_path) is None  # fmt: skip
    assert store.rows() == [explicit]


def test_upgrade_reads_history_from_ack_records_and_open_ack_drafts(attest_db, monkeypatch, node_signer):
    assert switches._ack_history() is False
    rec = _sign(_round_check(), node_signer).record
    _on(monkeypatch, "log_acknowledgements")
    service.create_ack_draft(site_id=SITE, target=rec["attestation_id"], origin="human", for_principal=OTHER)
    assert switches._ack_history() is True, "an open acknowledgement draft is history"


# -- approvals have no switch ---------------------------------------------------------------


def test_a_fresh_node_lists_nothing_approval_related_in_needs_you(attest_db, tmp_path, monkeypatch):
    from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
    from axiom.extensions.builtins.notifications.needs_you import needs_you

    monkeypatch.setattr(queue, "_state_dir", lambda: tmp_path)
    got = needs_you(ME, inbox_store=InMemoryInboxStore(), approvals_path=tmp_path / "approvals.json",
                    due_provider=lambda person, days: [])  # fmt: skip
    assert got["count"] == 0 and got["items"] == [] and got["top"] is None
    approve = next(s for s in got["sections"] if s["id"] == "approve")
    assert approve == {"id": "approve", "label": "To approve", "items": []}
    assert [s["id"] for s in got["sections"]] == ["due", "held", "approve", "sign", "cosign"]
    assert all(s["items"] == [] for s in got["sections"])


def test_a_switch_an_admin_turns_on_applies_on_their_next_page():
    """The cache spans one page's requests, never the admin's next page (journey SW-3)."""
    from axiom.extensions.builtins.attest import switches

    assert switches.TTL <= 1.0
