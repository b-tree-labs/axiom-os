# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cases — the decision unit: deterministic grouping, stable ids,
honest proposals, the attention cap, and the wire shapes."""

from __future__ import annotations

from axiom.extensions.builtins.receipts.brief import OversightItem
from axiom.extensions.builtins.receipts.cases import (
    CASES_CAP,
    case_id_for,
    case_payload,
    compose_cases,
)


def _item(entity="node-a", kind="backup", status="failed", action=None, site="s"):
    return OversightItem(
        entity_kind="node",
        entity_id=entity,
        claim_kind=kind,
        status=status,
        evidence=f"evidence for {entity}/{kind}",
        next_action=action,
        site=site,
    )


def test_one_case_per_troubled_entity():
    items = [
        _item("node-a", "backup", "failed", action="check the artifact"),
        _item("node-a", "canary", "stale", action="check the timer"),
        _item("node-b", "heartbeat", "stale"),
        _item("node-c", "backup", "green"),  # green never enters a case
    ]
    cases = compose_cases(items)
    assert [c.entity_id for c in cases] == ["node-a", "node-b"]
    a = cases[0]
    assert a.severity == "failed"  # worst member wins
    assert len(a.items) == 2
    assert a.proposal == ["check the artifact", "check the timer"]
    # backup FAILED is never explained by canary silence, so this really
    # is two problems and the headline says so.
    assert a.title == "node-a: 2 separate problems"
    b = cases[1]
    assert b.proposal == []  # no known next action → NO invented plan
    assert b.title == "node-b stopped reporting"


def test_case_ids_are_stable_across_composition():
    items = [_item("node-a", "backup", "failed")]
    first = compose_cases(items)[0].case_id
    again = compose_cases(list(reversed(items * 2)))[0].case_id
    assert first == again == case_id_for("s", "node", "node-a")
    # ... and differ per site: the same entity name at another site is
    # another case (site is an explicit axis).
    assert case_id_for("other", "node", "node-a") != first


def test_ordering_is_total_and_worst_first():
    items = [
        _item("node-z", "heartbeat", "stale"),
        _item("node-a", "service_health", "failed"),
        _item("node-m", "backup", "unproven"),
    ]
    got = [c.entity_id for c in compose_cases(items)]
    assert got == ["node-a", "node-z", "node-m"]


def test_brief_carries_capped_cases_and_honest_counts():
    import contextlib

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from axiom.extensions.builtins.receipts.brief import brief_payload, compose_brief
    from axiom.extensions.builtins.receipts.db_models import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    with contextlib.closing(session):
        items = [_item(f"node-{i}", "heartbeat", "stale") for i in range(5)]
        brief = compose_brief(session, items, snapshot=False)
        assert len(brief.cases) == CASES_CAP
        assert brief.counts["cases"] == 5
        assert brief.counts["cases_overflow"] == 2

        payload = brief_payload(brief)
        assert len(payload["cases"]) == CASES_CAP
        shape = payload["cases"][0]
        assert set(shape) == {
            "case_id",
            "site",
            "entity_kind",
            "entity_id",
            "title",
            "severity",
            "items",
            "proposal",
            "verdict",
            "precedent",
            "blast",
            "cause",
            "handling",
            "derivations",
            "options",
        }
        # Nothing decided yet reads as nothing decided — never as a
        # default decision.
        assert shape["verdict"] is None and shape["precedent"] is None
        # The undecided case says what may be decided about it, so the
        # surface renders the server's options instead of guessing. "fix"
        # is absent here because handling has not been computed for the
        # list, so no fix is known to apply — the surface is told nothing
        # rather than offered something that might not run.
        assert shape["handling"] is None
        # The window is always present as a list, even when these
        # hand-built items carry no arithmetic of their own — the claim
        # derivations come from the evaluator (see test_derivation.py).
        # The cause collapse is derived here, so it is in every case that
        # has one.
        assert isinstance(shape["derivations"], list)
        assert all(
            set(d) == {"subject", "claim", "inputs", "rule", "verify", "limit"}
            for d in shape["derivations"]
        )
        # Each offer says what it will do. "fix" is absent because no
        # handling was computed, so no fix is known to apply here.
        offered = {o["choice"]: o for o in shape["options"]}
        assert set(offered) == {"hold", "acknowledge", "discuss"}
        assert offered["hold"]["detail"]
        assert offered["acknowledge"]["detail"]
        # Reach is not computed for the list rendering — None means "not
        # computed here", which is different from a computed zero.
        assert shape["blast"] is None
        # The story, by contrast, IS composed with the case — it needs
        # only the claims, so every rendering can show it.
        assert shape["cause"]["summary"]
        assert shape["items"][0]["evidence"]  # receipts face-up in the case too


def test_case_payload_is_json_ready():
    import json

    case = compose_cases([_item(action="do the thing")])[0]
    round_tripped = json.loads(json.dumps(case_payload(case)))
    assert round_tripped["proposal"] == ["do the thing"]


# --- Plain language (founder feedback 2026-09-24: "too low level ...
# --- very idiomatic ... simplify the terminology, make it readable") ---

#: Words that are ours, not the reader's. A headline carrying any of
#: these is describing our data model instead of their situation.
INTERNAL_WORDS = (
    "claim",
    "non-green",
    "cadence",
    "3x",
    "oversight item",
    "entity",
    "verdict",
    "blast",
    "precedent",
)


def _titles(items):
    return [c.title for c in compose_cases(items)]


def test_a_headline_names_the_situation_not_our_data_model():
    """A person reads the headline first. It must say what happened to
    their thing, in their words."""
    for title in _titles(
        [
            _item("node-a", "heartbeat", "stale"),
            _item("node-a", "service_health", "stale"),
            _item("node-b", "backup", "failed"),
        ]
    ):
        low = title.lower()
        for word in INTERNAL_WORDS:
            assert word not in low, f"{title!r} leaks the internal word {word!r}"


def test_one_cause_gives_one_headline_not_a_list_of_symptoms():
    """The cause chain already knows a silent node explains its own stale
    claims. The headline must use that instead of listing both."""
    [title] = _titles(
        [
            _item("node-a", "heartbeat", "stale"),
            _item("node-a", "service_health", "stale"),
        ]
    )
    assert title == "node-a stopped reporting"


def test_two_unrelated_problems_say_so_rather_than_picking_one():
    """FAILED is never explained by silence, so this entity really does
    have two problems and the headline must not imply one."""
    [title] = _titles(
        [
            _item("node-a", "backup", "failed"),
            _item("node-a", "canary", "stale"),
        ]
    )
    assert "2 separate problems" in title
    assert "node-a" in title


def test_a_single_problem_reads_as_a_sentence():
    assert _titles([_item("node-a", "backup", "failed")]) == ["node-a: the backup did not work"]
    assert _titles([_item("node-a", "heartbeat", "stale")]) == ["node-a stopped reporting"]


def test_an_unknown_condition_still_reads_as_english():
    """A condition with no plain phrasing yet must degrade to a sentence,
    never to a raw field dump."""
    [title] = _titles([_item("node-a", "widget_depth", "unproven")])
    assert title == "node-a: widget depth is claimed with no evidence"


# --- The brief is what needs somebody (walkthrough step 1, 2026-09-25) ---


def test_a_decided_case_leaves_the_brief_and_is_counted_instead():
    """Founder: "decided cases shouldn't count or show on Today." A
    decision is on the record; putting the case back in the list asks the
    same question twice. It is COUNTED, not silently dropped."""
    import contextlib

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from axiom.extensions.builtins.receipts.brief import compose_brief
    from axiom.extensions.builtins.receipts.db_models import Base
    from axiom.extensions.builtins.receipts.verdicts import record_verdict

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    with contextlib.closing(session):
        items = [_item(f"node-{n}", "heartbeat", "stale") for n in "abc"]
        before = compose_brief(session, items, snapshot=False)
        assert before.counts["cases"] == 3
        assert before.counts["cases_decided"] == 0
        assert len(before.cases) == 3

        record_verdict(session, before.cases[0], chosen="hold", decider="@ben:s")
        session.commit()

        after = compose_brief(session, items, snapshot=False)
        assert after.counts["cases"] == 2, "a decided case no longer needs anybody"
        assert after.counts["cases_decided"] == 1
        assert [c.case_id for c in after.cases] == [c.case_id for c in before.cases[1:]], (
            "the decided one is gone from the list"
        )
        # Counted in the quiet line, so the drop is visible rather than silent.
        assert "1 case already decided" in after.quiet_line

    engine.dispose()
