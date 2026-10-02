# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The skills, and the contract every surface shares."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.lane.registry import UNMANAGED, Registry
from axiom.extensions.builtins.lane.skills import claim, doctor, list_lanes, register, release


class _Reg:
    def __init__(self):
        self.specs = []

    def register_skill(self, spec):
        self.specs.append(spec)


@pytest.fixture
def where(tmp_path):
    return tmp_path / "lanes.json"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A directory that answers as a git checkout."""
    root = tmp_path / "axiom-wt-example"
    root.mkdir()
    monkeypatch.setattr("axiom.extensions.builtins.lane.naming.repo_root", lambda _s: root)
    return root


def _run(fn, where, **params):
    return fn({"registry": where, "listening": {}, **params}, ctx=None)


# --- the shared contract ----------------------------------------------------


def test_every_skill_declares_caller_goal():
    """ADR-139: a caller can always say what it is trying to do, and can
    always discover that it may."""
    reg = _Reg()
    register(reg)

    assert reg.specs, "nothing registered"
    for spec in reg.specs:
        assert "caller_goal" in spec.inputs, f"{spec.name} does not accept caller_goal"


def test_every_skill_is_reachable_from_all_three_surfaces():
    reg = _Reg()
    register(reg)

    for spec in reg.specs:
        assert set(spec.surfaces) == {"cli", "mcp", "agent_tool"}, spec.name


def test_every_verb_that_writes_declares_that_it_writes():
    """`side_effects=False` is not "harmless". It is a positive declaration
    that the capability is a READ, and `approval_category` turns that
    straight into auto-approval:

        is_read_only(spec)      -> spec.side_effects is False
        approval_category(spec) -> READ if is_read_only else WRITE

    So a verb that writes shared state and declares False has declared a
    write to be a read. That is a misdeclaration of the contract, not a
    gate policy — and it is invisible, inherited by whatever copies it,
    and it undermines the only field the gate has.

    `hold` and `drop` write a registry other sessions read, so they declare
    True and cost a confirmation. The earlier argument for False — that a
    gated hold is a hold nobody takes — is a real cost and the wrong remedy:
    the contract has no way to say "a write cheap enough not to gate", and
    inventing one by lying in this field is not it.
    """
    reg = _Reg()
    register(reg)
    writers = {"lane.claim", "lane.release", "lane.hold", "lane.drop"}

    for spec in reg.specs:
        if spec.name in writers:
            assert spec.side_effects is True, f"{spec.name} writes and must say so"


def test_the_verb_that_can_destroy_something_declares_it():
    """A floor, deliberately narrow.

    The test above states a principle, and a principle can catch a harmless
    verb over-declaring. It CANNOT catch a destructive verb declaring
    False, because nothing here knows what is destructive except by being
    told — and that is the direction that actually hurts. So the one verb
    that can drop a database is named outright. Naming it is an inventory,
    which is a weaker kind of test; it is here because the alternative is
    no cover at all in the dangerous direction.
    """
    reg = _Reg()
    register(reg)
    by_name = {s.name: s for s in reg.specs}

    assert by_name["lane.release"].side_effects is True


# --- claim ------------------------------------------------------------------


def test_claim_derives_everything_from_the_checkout(where, checkout):
    r = _run(claim.run, where, root=str(checkout))

    assert r.ok
    assert r.value["lane"] == "axiom_wt_example"
    assert r.value["database"] == "axiom_lane_axiom_wt_example"
    assert r.value["isolated"] is True


def test_claim_hands_back_the_exports_a_shell_needs(where, checkout):
    r = _run(claim.run, where, root=str(checkout))
    exports = r.value["exports"]

    assert exports["AXIOM_LOCAL_PORT"] == str(r.value["front"])
    assert exports["AXIOM_DB_URL"].endswith(r.value["database"])


def test_a_second_claim_on_a_held_name_is_refused_and_says_who(where, checkout):
    _run(claim.run, where, root=str(checkout), owner="first")
    r = _run(claim.run, where, root=str(checkout), owner="second")

    assert not r.ok and "first" in r.errors[0]


def test_replace_is_explicit(where, checkout):
    _run(claim.run, where, root=str(checkout), owner="first")
    r = _run(claim.run, where, root=str(checkout), owner="second", replace=True)

    assert r.ok


def test_an_unmanaged_lane_gets_no_database_and_says_so(where, checkout):
    r = _run(claim.run, where, root=str(checkout), dsn_var=UNMANAGED)

    assert r.value["database"] is None
    assert r.value["isolated"] is False
    assert r.value["next"] == [], "nothing to migrate when nothing is isolated"


def test_claiming_outside_a_checkout_says_so(where, tmp_path, monkeypatch):
    monkeypatch.setattr("axiom.extensions.builtins.lane.naming.repo_root", lambda _s: None)
    r = _run(claim.run, where, root=str(tmp_path))

    assert not r.ok and "not inside a git checkout" in r.errors[0]


def test_two_checkouts_get_different_ports(where, tmp_path, monkeypatch):
    for folder in ("wt-one", "wt-two"):
        root = tmp_path / folder
        root.mkdir()
        monkeypatch.setattr("axiom.extensions.builtins.lane.naming.repo_root", lambda _s, r=root: r)
        _run(claim.run, where, root=str(root))

    lanes = Registry(where).all()
    ports = [p for lane in lanes.values() for p in lane.ports]

    assert len(lanes) == 2
    assert len(set(ports)) == len(ports), "two lanes were handed the same port"


# --- list -------------------------------------------------------------------


def test_list_reports_down_when_nothing_listens(where, checkout):
    _run(claim.run, where, root=str(checkout))
    r = _run(list_lanes.run, where)

    assert r.value["lanes"][0]["up"] is False


def test_list_reports_up_when_both_ports_are_heard(where, checkout):
    c = _run(claim.run, where, root=str(checkout))
    heard = {c.value["front"]: "x", c.value["api"]: "x"}
    r = list_lanes.run({"registry": where, "listening": heard}, ctx=None)

    assert r.value["lanes"][0]["up"] is True


def test_list_shows_the_reserved_ports_so_nobody_reaps_them(where):
    r = _run(list_lanes.run, where)
    reserved = {x["port"] for x in r.value["reserved"]}

    assert {8770, 8771, 8788} <= reserved


# --- release ----------------------------------------------------------------


def test_release_frees_the_name(where, checkout):
    _run(claim.run, where, root=str(checkout))
    r = _run(release.run, where, name="axiom_wt_example")

    assert r.ok and Registry(where).get("axiom_wt_example") is None


def test_release_never_drops_a_database_itself(where, checkout):
    """Irreversible, and belongs to the operator or TIDY's guarded path."""
    _run(claim.run, where, root=str(checkout))
    r = _run(release.run, where, name="axiom_wt_example", drop_database=True)

    assert r.value["dropped"] is False
    assert r.value["drop_command"].startswith("dropdb ")


def test_releasing_an_unknown_lane_says_so(where):
    r = _run(release.run, where, name="ghost")

    assert not r.ok and "ghost" in r.errors[0]


# --- doctor -----------------------------------------------------------------


def test_doctor_reports_rather_than_fails(where, tmp_path):
    """ok=False would turn a CI step red for a note, and the notes are the
    ones people should keep reading."""
    r = doctor.run(
        {"registry": where, "listening": {}, "workspace": tmp_path, "root": tmp_path}, ctx=None
    )

    assert r.ok is True
    assert "findings" in r.value and "text" in r.value


def test_doctor_is_silent_about_models_when_not_asked(where, tmp_path):
    r = doctor.run(
        {"registry": where, "listening": {}, "workspace": tmp_path, "root": tmp_path}, ctx=None
    )

    assert r.value["summary"] is None
    assert "no commentary" not in r.value["text"], "not asking is not a failure to report"


def test_doctor_flags_an_unmanaged_lane_as_a_note(where, checkout, tmp_path):
    _run(claim.run, where, root=str(checkout), dsn_var=UNMANAGED)
    r = doctor.run(
        {"registry": where, "listening": {}, "workspace": tmp_path, "root": tmp_path}, ctx=None
    )
    levels = {f["level"] for f in r.value["findings"]}

    assert "note" in levels
    assert "broken" not in levels, "an honest lack of isolation is not a breakage"
