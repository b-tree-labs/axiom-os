# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One cause, not N problems — and never a story the evidence does not
support. The rules collapse only what silence genuinely explains."""

from __future__ import annotations

from axiom.extensions.builtins.receipts.brief import OversightItem
from axiom.extensions.builtins.receipts.cause import explain


def _item(kind, status, *, at="2026-09-24T17:00:00+00:00"):
    return OversightItem(
        entity_kind="node",
        entity_id="node-a",
        claim_kind=kind,
        status=status,
        evidence=f"{kind} evidence",
        site="site-a",
        observed_at=at,
    )


def test_a_dead_reporter_explains_the_other_silence():
    chain = explain(
        [
            _item("heartbeat", "stale"),
            _item("service_health", "stale"),
            _item("versions", "unknown"),
        ]
    )
    roots = [s.claim_kind for s in chain.roots]
    assert roots == ["heartbeat"]
    explained = {s.claim_kind: s.explained_by for s in chain.steps if not s.is_root}
    assert explained == {"service_health": "heartbeat", "versions": "heartbeat"}
    # Plain wording (2026-09-24): the summary says what is wrong with
    # THEIR thing, not how many "claims" our model grouped.
    assert chain.summary().startswith("One problem, not 3.")
    # The reason is stated, not implied.
    step = next(s for s in chain.steps if s.claim_kind == "service_health")
    assert "nothing is arriving" in step.because


def test_content_faults_are_never_collapsed_into_silence():
    """A FAILED or UNPROVEN claim means a report ARRIVED and was wrong.
    Hiding it behind a dead reporter would hide a second fault."""
    chain = explain(
        [
            _item("heartbeat", "stale"),
            _item("backup", "failed"),
            _item("service_health", "unproven"),
        ]
    )
    roots = {s.claim_kind for s in chain.roots}
    assert roots == {"heartbeat", "backup", "service_health"}
    assert "3 separate problems" in chain.summary()
    assert "Fixing one will not fix the other." in chain.summary()


def test_a_healthy_reporter_explains_nothing():
    chain = explain([_item("backup", "failed"), _item("service_health", "stale")])
    assert all(s.is_root for s in chain.steps)  # no heartbeat claim at all
    chain2 = explain([_item("heartbeat", "green"), _item("service_health", "stale")])
    assert all(s.is_root for s in chain2.steps)  # reporter is fine; silence is local


def test_roots_come_first_so_the_thing_to_fix_reads_first():
    chain = explain([_item("service_health", "stale"), _item("heartbeat", "stale")])
    assert chain.steps[0].claim_kind == "heartbeat"
    assert chain.steps[0].is_root


def test_a_single_claim_reads_as_one_claim_not_a_chain():
    chain = explain([_item("backup", "failed")])
    assert chain.summary() == "node-a: the backup did not work."


def test_payload_is_json_ready_and_carries_the_reason():
    import json

    chain = explain([_item("heartbeat", "stale"), _item("service_health", "stale")])
    payload = json.loads(json.dumps(chain.payload()))
    assert set(payload) == {"steps", "summary"}
    assert set(payload["steps"][0]) == {
        "claim_kind",
        "status",
        "evidence",
        "observed_at",
        "explained_by",
        "because",
        # what the step MEANS, so a surface renders a sentence instead of
        # assembling one out of claim_kind and status.
        "reads",
        "is_root",
    }
    # Timing comes from the server, so a surface never uses a client clock.
    assert payload["steps"][0]["observed_at"].startswith("2026-09-24T17:00")


def test_no_claims_is_no_story():
    assert explain([]).summary() == ""


# --- Plain language (founder feedback 2026-09-24) ---


def _chain(*specs):
    from axiom.extensions.builtins.receipts.cause import explain

    return explain(
        [
            OversightItem(
                entity_kind="node",
                entity_id="node-a",
                claim_kind=kind,
                status=status,
                evidence="e",
                site="s",
            )
            for kind, status in specs
        ]
    )


def test_the_story_is_told_in_the_readers_words():
    """The summary is the first sentence a person reads about the case.
    It must not be made of our field names and status enums."""
    chain = _chain(("heartbeat", "stale"), ("service_health", "stale"))
    text = chain.summary().lower()
    for internal in ("claim", "(stale)", "independent causes", "service_health"):
        assert internal not in text, f"summary leaks {internal!r}: {chain.summary()!r}"
    assert chain.summary() == (
        "One problem, not 2. node-a stopped reporting — the rest follows from that."
    )


def test_each_step_reads_as_a_sentence_on_its_own():
    """The surface should not have to build 'heartbeat is stale' out of
    two fields; the server says what the step means."""
    chain = _chain(("heartbeat", "stale"), ("service_health", "stale"))
    reads = [s.payload()["reads"] for s in chain.steps]
    assert reads[0] == "node-a stopped reporting"
    assert "claim" not in " ".join(reads).lower()


def test_two_real_problems_say_that_fixing_one_will_not_fix_the_other():
    chain = _chain(("backup", "failed"), ("canary", "stale"))
    text = chain.summary()
    assert "2 separate problems" in text
    assert "Fixing one will not fix the other." in text


def test_a_lone_problem_is_stated_not_counted():
    assert _chain(("backup", "failed")).summary() == "node-a: the backup did not work."
