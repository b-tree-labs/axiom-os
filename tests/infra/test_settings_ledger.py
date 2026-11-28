# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Settings ledger, chunk 1 (ADR-284; prd-settings ST-11, ST-16, ST-18).

The ``node`` layer of settings is an append-only, hash-chained ledger. This chunk is its
file-backed form: the entry format and chain, compare-and-swap on a key's revision, one writer
per section, schema checks, the ``site_settings`` source, and fail-closed on damage. The Postgres
backend, signatures and the offsite anchor are later chunks.

Each test carries the requirement it proves. The module is imported inside a fixture so every
test fails for its own reason while the module does not exist.

* LS-1  ST-11  entries are numbered, linked by hash, and the chain verifies
* LS-2  ST-11  a changed byte breaks verification and names the entry
* LS-3  ST-11  a write with a stale revision is refused and appends nothing
* LS-4  ST-11  two writers holding the same revision: exactly one wins
* LS-5  ST-16  a writer cannot write another section
* LS-6  ST-16  a section must be declared before it is written
* LS-7  ST-16  a value that breaks the section's schema is refused and appends nothing
* LS-8  ST-11  the effective value is the latest entry; an unset falls back to nothing
* LS-9  ST-11  attest reads its gate pairs from the ledger, as the authoritative ``site_settings`` source
* LS-10 ST-11  a pair set in the ledger confers its signing role
* LS-11 ST-18  a damaged ledger reads every key as unset, so no pair confers a role

Chunk 2a, authority keys (ST-15; ADR-284 item 6 and B5). The ledger asks a verifier, registered
by the signed-record extension, whether evidence signs one exact change:

* LS-12 ST-15  an authority key is refused without evidence
* LS-13 ST-15  evidence the verifier rejects is refused and appends nothing
* LS-14 ST-15  accepted evidence is kept on the entry, and the verifier is told the exact change
* LS-15 ST-15  evidence for one change cannot authorise another value or another revision
* LS-16 ST-15  a forged entry with a valid chain but no valid evidence is ignored and reported
* LS-17 ST-15  a revision that does not follow the one before breaks the chain
* LS-18 ST-15  with no verifier, an authority entry is ignored and a write is refused
* LS-19 ST-15  evidence is checked again on every read, so a record that goes away stops the entry
* LS-20 ST-15  a verified entry is not re-asked within the verification window, and is after it
* LS-21 B3     an entry a later release added a field to is still read by this one
* LS-22 ST-15  evidence is bound to the scope of the ledger, so another site's record is not evidence
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

GATE_ROLES_SCHEMA = {
    "type": "object",
    "additionalProperties": {"type": "string"},
}
SECTION_SCHEMA = {"gate_roles": GATE_ROLES_SCHEMA, "interval": {"type": "integer", "minimum": 1}}


@pytest.fixture()
def sl():
    from axiom.infra import settings_ledger

    return settings_ledger


@pytest.fixture()
def ledger(sl, tmp_path: Path):
    led = sl.SettingsLedger(tmp_path / "settings.ledger.jsonl")
    led.declare_section("attest", schema=SECTION_SCHEMA)
    led.declare_section("ops_log", schema={"interval": {"type": "integer", "minimum": 1}})
    return led


@pytest.fixture(autouse=True)
def _no_leftover_source():
    from axiom.extensions.builtins.attest import site_settings

    site_settings.unregister()
    yield
    site_settings.unregister()


def test_ls1_entries_are_numbered_linked_and_verify(ledger):
    w = ledger.writer("attest", actor="@owner")
    first = w.set("interval", 5, expected_revision=0)
    second = w.set("interval", 6, expected_revision=1)

    assert (first.seq, second.seq) == (1, 2)
    assert second.prev_hash == first.hash
    report = ledger.verify()
    assert report.ok and report.records == 2


def test_ls2_a_changed_byte_breaks_verification_and_names_the_entry(ledger, tmp_path):
    w = ledger.writer("attest", actor="@owner")
    w.set("interval", 5, expected_revision=0)
    w.set("interval", 6, expected_revision=1)

    path = tmp_path / "settings.ledger.jsonl"
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    first["value"] = 500
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")

    report = ledger.verify()
    assert not report.ok
    assert report.broken_at == 1


def test_ls3_a_stale_revision_is_refused_and_appends_nothing(ledger):
    w = ledger.writer("attest", actor="@owner")
    w.set("interval", 5, expected_revision=0)

    with pytest.raises(ledger_error(ledger, "StaleRevision")):
        w.set("interval", 9, expected_revision=0)

    assert ledger.revision("attest", "interval") == 1
    assert ledger.value("attest", "interval") == 5
    assert ledger.verify().records == 1


def test_ls4_two_writers_with_one_revision_one_wins(ledger):
    results: list[str] = []
    barrier = threading.Barrier(2)

    def write(actor: str, value: int) -> None:
        w = ledger.writer("attest", actor=actor)
        barrier.wait()
        try:
            w.set("interval", value, expected_revision=0)
            results.append("won")
        except Exception as exc:  # noqa: BLE001
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=write, args=(f"@m{i}", i + 1)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["StaleRevision", "won"]
    assert ledger.verify().records == 1


def test_ls5_a_writer_cannot_write_another_section(ledger):
    w = ledger.writer("attest", actor="@owner")

    with pytest.raises(ledger_error(ledger, "SectionNotOwned")):
        w.write("ops_log", "interval", 5, expected_revision=0)

    assert ledger.verify().records == 0
    assert ledger.value("ops_log", "interval") is None


def test_ls6_a_section_is_declared_before_it_is_written(sl, tmp_path):
    led = sl.SettingsLedger(tmp_path / "settings.ledger.jsonl")

    with pytest.raises(sl.UnknownSection):
        led.writer("nobody_declared_this", actor="@owner")


def test_ls7_a_value_that_breaks_the_schema_is_refused(ledger):
    w = ledger.writer("attest", actor="@owner")

    with pytest.raises(ledger_error(ledger, "InvalidValue")):
        w.set("interval", 0, expected_revision=0)  # minimum is 1
    with pytest.raises(ledger_error(ledger, "InvalidValue")):
        w.set("gate_roles", {"reactor-operator": 7}, expected_revision=0)  # values are strings
    with pytest.raises(ledger_error(ledger, "InvalidValue")):
        w.set("not_declared", 1, expected_revision=0)  # a key the section does not declare

    assert ledger.verify().records == 0


def test_ls8_the_latest_entry_is_the_value_and_unset_removes_it(ledger):
    w = ledger.writer("attest", actor="@owner")
    w.set("interval", 5, expected_revision=0)
    w.set("interval", 8, expected_revision=1)
    assert ledger.value("attest", "interval") == 8

    w.unset("interval", expected_revision=2)
    assert ledger.value("attest", "interval") is None
    assert ledger.value("attest", "interval", default=30) == 30
    assert ledger.revision("attest", "interval") == 3


def test_ls9_attest_reads_its_gate_pairs_through_site_settings(ledger):
    from axiom.extensions.builtins.attest import site_settings
    from axiom.extensions.builtins.attest.roles import GATE_PAIRS_KEY, gate_pairs

    pairs = {"reactor-operator": "reactor_operator", "senior-reactor-operator": "senior_reactor_operator"}
    ledger.writer("attest", actor="@owner").set("gate_roles", pairs, expected_revision=0)
    site_settings.register_authoritative(ledger.as_site_settings_source())

    assert GATE_PAIRS_KEY == "attest.gate_roles"
    assert site_settings.authoritative_value("any-site", GATE_PAIRS_KEY, None) == pairs
    assert site_settings.value("any-site", GATE_PAIRS_KEY, None) is None  # not a plain source
    assert gate_pairs("any-site") == pairs


def test_ls10_a_pair_set_in_the_ledger_confers_its_signing_role(ledger, tmp_path):
    from axiom.extensions.builtins.attest import site_settings
    from axiom.extensions.builtins.attest.roles import roles_for

    state = tmp_path / "state"
    state.mkdir()
    site_settings.register_authoritative(ledger.as_site_settings_source())
    assert roles_for("@op", "s", state_dir=state, gate_roles=("reactor-operator",)) == ()

    ledger.writer("attest", actor="@owner").set(
        "gate_roles", {"reactor-operator": "reactor_operator"}, expected_revision=0
    )
    assert roles_for("@op", "s", state_dir=state, gate_roles=("reactor-operator",)) == ("reactor_operator",)

    ledger.writer("attest", actor="@owner").unset("gate_roles", expected_revision=1)
    assert roles_for("@op", "s", state_dir=state, gate_roles=("reactor-operator",)) == ()


def test_ls11_a_damaged_ledger_reads_every_key_as_unset(ledger, tmp_path):
    from axiom.extensions.builtins.attest import site_settings
    from axiom.extensions.builtins.attest.roles import gate_pairs

    pairs = {"reactor-operator": "reactor_operator"}
    ledger.writer("attest", actor="@owner").set("gate_roles", pairs, expected_revision=0)
    site_settings.register_authoritative(ledger.as_site_settings_source())
    assert gate_pairs("s") == pairs

    path = tmp_path / "settings.ledger.jsonl"
    entry = json.loads(path.read_text().splitlines()[0])
    entry["value"] = {"reactor-operator": "reactor_operator", "intruder": "senior_reactor_operator"}
    path.write_text(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")

    assert not ledger.verify().ok
    assert gate_pairs("s") == {}  # damage never confers authority
    assert ledger.damage() is not None  # and says why


def ledger_error(ledger, name: str):
    """The exception class by name, from the module the ledger came from."""
    import importlib

    return getattr(importlib.import_module(type(ledger).__module__), name)


# -- chunk 2a: authority keys ------------------------------------------------------------------

PAIRS = {"reactor-operator": "reactor_operator"}


class FakeVerifier:
    """Accepts evidence only for the one change it was told about, and records every question."""

    def __init__(self, sl):
        self.sl = sl
        self.accepts: dict[str, str] = {}  # evidence -> change digest it signs
        self.asked: list[dict] = []

    def sign(self, evidence: str, **change) -> None:
        self.accepts[evidence] = self.sl.change_digest(**change)

    def verify(self, *, section, key, op, value, revision, evidence, scope=""):
        change = {"section": section, "key": key, "op": op, "value": value, "revision": revision}
        if scope:
            change["scope"] = scope
        self.asked.append({**change, "evidence": evidence})
        if evidence not in self.accepts:
            return f"no signed record {evidence!r}"
        if self.accepts[evidence] != self.sl.change_digest(**change):
            return f"record {evidence!r} signs a different change"
        return None


@pytest.fixture()
def authority(sl, tmp_path: Path):
    led = sl.SettingsLedger(tmp_path / "settings.ledger.jsonl", verification_ttl=0)  # ask on every read
    led.declare_section("attest", schema=SECTION_SCHEMA, authority=("gate_roles",))
    verifier = FakeVerifier(sl)
    sl.set_authority_verifier(verifier)
    yield led, verifier
    sl.reset_authority_verifier()


def _raw_append(path: Path, sl, **fields) -> None:
    """What someone with write access to the file and a copy of the format can do."""
    lines = path.read_text().splitlines() if path.exists() else []
    prev = json.loads(lines[-1])["hash"] if lines else sl.GENESIS
    body = {
        "seq": len(lines) + 1, "ts": "2026-11-24T00:00:00+00:00", "section": "attest",
        "key": "gate_roles", "op": "set", "value": PAIRS, "actor": "@intruder",
        "key_revision": 1, "prev_hash": prev, "evidence": None, **fields,
    }  # fmt: skip
    record = {**body, "hash": sl._hash(body, prev)}
    with open(path, "a") as fh:
        fh.write(sl._canonical(record) + "\n")


def test_ls12_an_authority_key_is_refused_without_evidence(authority, sl):
    led, _ = authority
    with pytest.raises(sl.AuthorityRequired):
        led.writer("attest", actor="@owner").set("gate_roles", PAIRS, expected_revision=0)
    assert led.verify().records == 0


def test_ls13_evidence_the_verifier_rejects_is_refused(authority, sl):
    led, _ = authority
    with pytest.raises(sl.AuthorityRefused, match="no signed record"):
        led.writer("attest", actor="@owner").set(
            "gate_roles", PAIRS, expected_revision=0, evidence="att-nobody-signed"
        )
    assert led.verify().records == 0
    assert led.value("attest", "gate_roles") is None


def test_ls14_accepted_evidence_is_kept_and_the_verifier_is_told_the_exact_change(authority, sl):
    led, verifier = authority
    verifier.sign("att-1", section="attest", key="gate_roles", op="set", value=PAIRS, revision=1)

    entry = led.writer("attest", actor="@owner").set(
        "gate_roles", PAIRS, expected_revision=0, evidence="att-1"
    )

    assert entry.evidence == "att-1"
    assert led.value("attest", "gate_roles") == PAIRS
    assert verifier.asked[0] == {
        "section": "attest", "key": "gate_roles", "op": "set", "value": PAIRS,
        "revision": 1, "evidence": "att-1",
    }  # fmt: skip


def test_ls15_evidence_for_one_change_cannot_authorise_another(authority, sl):
    led, verifier = authority
    verifier.sign("att-1", section="attest", key="gate_roles", op="set", value=PAIRS, revision=1)
    w = led.writer("attest", actor="@owner")

    wider = {**PAIRS, "senior-reactor-operator": "senior_reactor_operator"}
    with pytest.raises(sl.AuthorityRefused, match="different change"):
        w.set("gate_roles", wider, expected_revision=0, evidence="att-1")  # another value

    w.set("gate_roles", PAIRS, expected_revision=0, evidence="att-1")
    w.unset("gate_roles", expected_revision=1, evidence=_sign_unset(verifier, revision=2))
    with pytest.raises(sl.AuthorityRefused, match="different change"):
        w.set("gate_roles", PAIRS, expected_revision=2, evidence="att-1")  # replayed at revision 3


def _sign_unset(verifier, *, revision: int) -> str:
    evidence = f"att-unset-{revision}"
    verifier.sign(evidence, section="attest", key="gate_roles", op="unset", value=None, revision=revision)
    return evidence


def test_ls16_a_forged_entry_with_a_valid_chain_is_ignored_and_reported(authority, sl, tmp_path):
    from axiom.extensions.builtins.attest import site_settings
    from axiom.extensions.builtins.attest.roles import gate_pairs

    led, _ = authority
    site_settings.register_authoritative(led.as_site_settings_source())
    _raw_append(tmp_path / "settings.ledger.jsonl", sl, evidence=None)
    _raw_append(tmp_path / "settings.ledger.jsonl", sl, evidence="att-forged", key_revision=2)

    assert led.verify().ok  # the chain itself is intact: that is the attack
    assert led.value("attest", "gate_roles") is None
    assert gate_pairs("s") == {}
    reasons = [u.reason for u in led.unverified()]
    assert len(reasons) == 2 and "evidence" in reasons[0]


def test_ls17_a_revision_that_does_not_follow_breaks_the_chain(authority, sl, tmp_path):
    led, verifier = authority
    verifier.sign("att-1", section="attest", key="gate_roles", op="set", value=PAIRS, revision=1)
    led.writer("attest", actor="@owner").set("gate_roles", PAIRS, expected_revision=0, evidence="att-1")

    _raw_append(tmp_path / "settings.ledger.jsonl", sl, evidence="att-1", key_revision=1)  # not 2

    assert not led.verify().ok
    assert led.damage() is not None


def test_ls18_with_no_verifier_an_authority_entry_is_ignored_and_a_write_refused(sl, tmp_path):
    led = sl.SettingsLedger(tmp_path / "settings.ledger.jsonl")
    led.declare_section("attest", schema=SECTION_SCHEMA, authority=("gate_roles",))
    sl.reset_authority_verifier()
    _raw_append(tmp_path / "settings.ledger.jsonl", sl, evidence="att-1")

    assert led.value("attest", "gate_roles") is None  # nobody can vouch for it
    with pytest.raises(sl.AuthorityRefused, match="no verifier"):
        led.writer("attest", actor="@owner").set(
            "gate_roles", PAIRS, expected_revision=1, evidence="att-1"
        )
    led.writer("attest", actor="@owner").set("interval", 5, expected_revision=0)  # not authority
    assert led.value("attest", "interval") == 5


def test_ls19_evidence_is_checked_again_on_every_read(authority, sl):
    led, verifier = authority
    verifier.sign("att-1", section="attest", key="gate_roles", op="set", value=PAIRS, revision=1)
    led.writer("attest", actor="@owner").set("gate_roles", PAIRS, expected_revision=0, evidence="att-1")
    assert led.value("attest", "gate_roles") == PAIRS

    del verifier.accepts["att-1"]  # the signed record is gone

    assert led.value("attest", "gate_roles") is None
    assert [u.seq for u in led.unverified()] == [1]


def test_ls20_a_verified_entry_is_not_re_asked_within_the_window_and_is_after_it(sl, tmp_path):
    now = [1000.0]
    led = sl.SettingsLedger(tmp_path / "settings.ledger.jsonl", verification_ttl=30, clock=lambda: now[0])
    led.declare_section("attest", schema=SECTION_SCHEMA, authority=("gate_roles",))
    verifier = FakeVerifier(sl)
    sl.set_authority_verifier(verifier)
    try:
        verifier.sign("att-1", section="attest", key="gate_roles", op="set", value=PAIRS, revision=1)
        led.writer("attest", actor="@owner").set("gate_roles", PAIRS, expected_revision=0, evidence="att-1")
        asked = len(verifier.asked)  # the write asked once

        for _ in range(5):
            assert led.value("attest", "gate_roles") == PAIRS
        assert len(verifier.asked) == asked + 1  # the first read asked; the other four did not

        now[0] += 31  # the window passes
        del verifier.accepts["att-1"]  # and the record has gone
        assert led.value("attest", "gate_roles") is None
        assert len(verifier.asked) == asked + 2
    finally:
        sl.reset_authority_verifier()


def test_ls21_an_entry_a_later_release_added_a_field_to_is_still_read(sl, tmp_path):
    path = tmp_path / "settings.ledger.jsonl"
    led = sl.SettingsLedger(path)
    led.declare_section("attest", schema=SECTION_SCHEMA)
    led.writer("attest", actor="@owner").set("interval", 5, expected_revision=0)

    # a later release writes an entry carrying a field this release does not know, hashed correctly
    record = json.loads(path.read_text().splitlines()[0])
    body = {k: v for k, v in record.items() if k != "hash"}
    body["added_in_a_later_release"] = "x"
    path.write_text(sl._canonical({**body, "hash": sl._hash(body, body["prev_hash"])}) + "\n")

    assert led.damage() is None, led.damage()
    assert led.verify().ok
    assert led.value("attest", "interval") == 5


def test_ls22_evidence_is_bound_to_the_scope_of_the_ledger(sl, tmp_path):
    here = dict(section="attest", key="gate_roles", op="set", value=PAIRS, revision=1)
    assert sl.change_digest(scope="site-a", **here) != sl.change_digest(scope="site-b", **here)

    led = sl.SettingsLedger(tmp_path / "a.jsonl", scope="site-a", verification_ttl=0)
    led.declare_section("attest", schema=SECTION_SCHEMA, authority=("gate_roles",))
    verifier = FakeVerifier(sl)
    sl.set_authority_verifier(verifier)
    try:
        verifier.sign("att-b", scope="site-b", **here)  # signed for the other site
        with pytest.raises(sl.AuthorityRefused, match="different change"):
            led.writer("attest", actor="@owner").set("gate_roles", PAIRS, expected_revision=0, evidence="att-b")

        verifier.sign("att-a", scope="site-a", **here)
        led.writer("attest", actor="@owner").set("gate_roles", PAIRS, expected_revision=0, evidence="att-a")
        assert verifier.asked[-1]["scope"] == "site-a"
        assert led.value("attest", "gate_roles") == PAIRS
    finally:
        sl.reset_authority_verifier()
