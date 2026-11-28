# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Records the system stores become things the system can answer from.

Ingest elsewhere is artifact-oriented — a file arrives and is indexed. That
leaves the rows untouched, so a question whose answer sits in a table gets "I
don't have that". These tests pin the two decisions that make harvesting
survivable: coverage that does not depend on anyone remembering to register, and
scope that is never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from axiom.rag import harvest


@dataclass
class Experiment:
    id: int = 7
    name: str = "rod drop A"
    operator: str = "sam"
    notes: str = "ran clean"
    created_at: str = "2026-09-15T00:00:00Z"
    api_key: str = "sk-do-not-publish"
    _private: str = "hidden"


def _scope(_obj):
    return harvest.HarvestScope(corpus="rag-org", owner="site-a")


@pytest.fixture(autouse=True)
def _clean_renderers():
    harvest.clear_renderers()
    yield
    harvest.clear_renderers()


# --- coverage without registration ------------------------------------------

def test_an_unregistered_record_still_renders():
    """The point of registration-free coverage: nobody had to think of this.

    Opt-in would mean the corpus covers only what someone remembered, and the
    gap is invisible — you cannot see the answer that was never retrievable.
    """
    text = harvest.render_entity(Experiment())
    assert "Experiment record." in text
    assert "name: rod drop A" in text
    assert "operator: sam" in text


def test_a_hand_written_renderer_wins_when_registered():
    harvest.register_renderer("experiment", lambda o: f"Experiment {o.name}, by {o.operator}.")
    assert harvest.render_entity(Experiment()) == "Experiment rod drop A, by sam."


def test_bookkeeping_columns_are_not_rendered():
    """Timestamps and ids dilute the card and cost embedding quality."""
    text = harvest.render_entity(Experiment())
    assert "created_at" not in text
    assert "id: 7" not in text


def test_a_credential_shaped_column_is_never_rendered():
    """A harvested card goes into a corpus. A secret in one is a secret published."""
    text = harvest.render_entity(Experiment())
    assert "sk-do-not-publish" not in text
    assert "api_key" not in text


def test_private_attributes_are_not_rendered():
    assert "hidden" not in harvest.render_entity(Experiment())


# --- identity and keying ----------------------------------------------------

def test_the_key_is_stable_so_re_harvesting_updates_in_place():
    """UNIQUE(source_path, corpus) is what makes harvest-on-write affordable."""
    a = harvest.build_card(Experiment(), _scope)
    b = harvest.build_card(Experiment(name="renamed"), _scope)
    assert a.source_path == b.source_path == "entity://experiment/7"


def test_a_record_with_no_identity_is_skipped_not_duplicated():
    """Without a stable key every write would append another copy."""

    @dataclass
    class Anonymous:
        note: str = "no id here"

    assert harvest.build_card(Anonymous(), _scope) is None


# --- scope is declared, never guessed ---------------------------------------

def test_an_undeclared_scope_refuses_rather_than_guessing():
    """A wrong guess puts one tenant's records in another tenant's answers."""
    with pytest.raises(harvest.ScopeUndeclared, match="refusing to guess"):
        harvest.build_card(Experiment(), lambda _o: None)


def test_the_declared_scope_is_carried_onto_the_card():
    card = harvest.build_card(Experiment(), _scope)
    assert card.scope.corpus == "rag-org"
    assert card.scope.owner == "site-a"


def test_the_card_names_the_record_readably():
    card = harvest.build_card(Experiment(), _scope)
    assert card.title == "Experiment 7"
    assert card.entity_type == "experiment"
    assert card.entity_id == "7"


# --- the durable hand-off ---------------------------------------------------
#
# Harvesting cannot run inside the writing transaction: the write must not fail
# because an embedding endpoint is slow, and a record is not real until it
# commits. So intent is recorded at commit and acted on afterwards.

def _intent(eid="7", op="upsert", etype="experiment", corpus="rag-org"):
    from axiom.rag.harvest_queue import HarvestIntent

    return HarvestIntent(entity_type=etype, entity_id=eid, op=op, corpus=corpus,
                         owner="site-a")


def test_a_queued_intent_survives_to_be_read(tmp_path):
    from axiom.rag import harvest_queue as q

    path = tmp_path / "harvest.jsonl"
    q.enqueue(path, _intent())
    pending = q.read_pending(path)
    assert len(pending) == 1
    assert pending[0].entity_id == "7" and pending[0].op == "upsert"


def test_a_record_touched_repeatedly_is_harvested_once_at_its_final_state(tmp_path):
    """A burst of writes should cost one harvest, not five."""
    from axiom.rag import harvest_queue as q

    path = tmp_path / "harvest.jsonl"
    for _ in range(5):
        q.enqueue(path, _intent())
    assert len(q.read_pending(path)) == 1


def test_a_delete_supersedes_an_earlier_upsert(tmp_path):
    """Keyed without the op so the pair collapses to the delete, not to both.

    Harvesting an upsert after the record was deleted would leave the corpus
    answering from a row the database no longer has.
    """
    from axiom.rag import harvest_queue as q

    path = tmp_path / "harvest.jsonl"
    q.enqueue(path, _intent(op="upsert"))
    q.enqueue(path, _intent(op="delete"))
    pending = q.read_pending(path)
    assert len(pending) == 1 and pending[0].op == "delete"


def test_one_unreadable_line_does_not_strand_the_rest(tmp_path, caplog):
    """A queue that quietly drops work looks exactly like a queue with none."""
    import logging

    from axiom.rag import harvest_queue as q

    path = tmp_path / "harvest.jsonl"
    q.enqueue(path, _intent(eid="1"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    q.enqueue(path, _intent(eid="2"))

    with caplog.at_level(logging.WARNING, logger="axiom.rag.harvest_queue"):
        pending = q.read_pending(path)

    assert {p.entity_id for p in pending} == {"1", "2"}, "a healthy entry was lost"
    assert any("unreadable" in r.message for r in caplog.records), (
        "the dropped entry was not reported"
    )


def test_queueing_never_raises_into_a_committed_write(tmp_path, caplog, monkeypatch):
    """The caller already committed. Failing now would undo real work."""
    import logging

    from axiom.rag import harvest_queue as q

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("axiom.infra.state.locked_append_jsonl", _boom)
    with caplog.at_level(logging.ERROR, logger="axiom.rag.harvest_queue"):
        q.enqueue(tmp_path / "harvest.jsonl", _intent())  # must not raise

    blob = " ".join(r.message for r in caplog.records)
    assert "harvest" in blob and "stands" in blob, (
        f"the failure was not explained: {[r.message for r in caplog.records]}"
    )


def test_a_missing_queue_is_empty_not_an_error(tmp_path):
    from axiom.rag import harvest_queue as q

    assert q.read_pending(tmp_path / "never-written.jsonl") == []
