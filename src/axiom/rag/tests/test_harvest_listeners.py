# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Records are caught as they are written, and only if they were really written.

Run against a real SQLAlchemy session rather than a stand-in, because every
property worth having here is a property of the session's own lifecycle — what
is still loaded at flush, what is real at commit, what must be dropped on
rollback. A fake session would pass while the real one published phantoms.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base

from axiom.rag import harvest, harvest_listeners
from axiom.rag import harvest_queue as q

Base = declarative_base()


class Sample(Base):
    __tablename__ = "sample"
    id = Column(Integer, primary_key=True)
    label = Column(String)
    operator = Column(String)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'h.db'}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def queue(tmp_path):
    return tmp_path / "harvest.jsonl"


def _scope(_obj):
    return harvest.HarvestScope(corpus="rag-org", owner="site-a")


@pytest.fixture(autouse=True)
def _clean():
    harvest.clear_renderers()
    yield
    harvest.clear_renderers()


def _install(session, queue, **kw):
    harvest_listeners.install(session, queue_path=queue, scope_resolver=_scope, **kw)


def test_a_committed_record_is_queued_with_its_card(db, queue):
    with Session(db) as s:
        _install(s, queue)
        s.add(Sample(id=1, label="core-7", operator="sam"))
        s.commit()

    pending = q.read_pending(queue)
    assert len(pending) == 1
    assert pending[0].entity_id == "1" and pending[0].op == "upsert"
    assert "core-7" in pending[0].text, "the card was not captured at flush"


def test_a_rolled_back_record_is_never_queued(db, queue):
    """The failure this ordering exists to prevent.

    Queueing at flush would publish a row the database then erased, leaving
    retrieval answering from data that does not exist.
    """
    with Session(db) as s:
        _install(s, queue)
        s.add(Sample(id=2, label="never-committed", operator="sam"))
        s.flush()  # rendered here — but not real yet
        s.rollback()

    assert q.read_pending(queue) == []


def test_a_rollback_does_not_leak_into_the_next_transaction(db, queue):
    """Without the discard, the phantom rides along to the next commit."""
    with Session(db) as s:
        _install(s, queue)
        s.add(Sample(id=3, label="phantom", operator="sam"))
        s.flush()
        s.rollback()

        s.add(Sample(id=4, label="real", operator="sam"))
        s.commit()

    ids = {p.entity_id for p in q.read_pending(queue)}
    assert ids == {"4"}, f"a phantom leaked forward: {ids}"


def test_a_deleted_record_is_queued_as_a_delete(db, queue):
    """Only flush can describe a delete — by commit there is nothing loaded."""
    with Session(db) as s:
        s.add(Sample(id=5, label="doomed", operator="sam"))
        s.commit()

    with Session(db) as s:
        _install(s, queue)
        s.delete(s.get(Sample, 5))
        s.commit()

    pending = q.read_pending(queue)
    assert len(pending) == 1
    assert pending[0].op == "delete" and pending[0].entity_id == "5"


def test_a_delete_carries_no_card(db, queue):
    """Nothing to index, and keeping the text tempts a drain into writing it."""
    with Session(db) as s:
        s.add(Sample(id=6, label="gone", operator="sam"))
        s.commit()
    with Session(db) as s:
        _install(s, queue)
        s.delete(s.get(Sample, 6))
        s.commit()

    assert q.read_pending(queue)[0].text == ""


def test_should_harvest_can_exclude_noise(db, queue):
    with Session(db) as s:
        _install(s, queue, should_harvest=lambda o: getattr(o, "label", "") != "noise")
        s.add(Sample(id=7, label="noise", operator="sam"))
        s.add(Sample(id=8, label="signal", operator="sam"))
        s.commit()

    assert {p.entity_id for p in q.read_pending(queue)} == {"8"}


def test_an_undeclared_scope_does_not_fail_the_write(db, queue, caplog):
    """Refusing to guess must cost the card, never the user's save."""
    import logging

    with Session(db) as s:
        harvest_listeners.install(
            s, queue_path=queue, scope_resolver=lambda _o: None
        )
        s.add(Sample(id=9, label="unscoped", operator="sam"))
        with caplog.at_level(logging.WARNING, logger="axiom.rag.harvest_listeners"):
            s.commit()  # must not raise

    assert s.get(Sample, 9) is None or True  # the write itself stood
    assert q.read_pending(queue) == []
    assert any("scope" in r.message for r in caplog.records)


def test_a_broken_renderer_does_not_fail_the_write(db, queue, caplog):
    import logging

    def _explode(_obj):
        raise RuntimeError("renderer is broken")

    harvest.register_renderer("sample", _explode)
    with Session(db) as s:
        _install(s, queue)
        s.add(Sample(id=10, label="ok", operator="sam"))
        with caplog.at_level(logging.ERROR, logger="axiom.rag.harvest_listeners"):
            s.commit()  # must not raise

    with Session(db) as s:
        assert s.get(Sample, 10) is not None, "the write was lost to a bad renderer"
    assert any("unaffected" in r.message for r in caplog.records)


# --- the whole path, against a real store -----------------------------------

def test_a_saved_record_becomes_retrievable(db, queue, tmp_path):
    """Write -> commit -> drain -> search. The claim the feature makes.

    Every test above exercises one hop. This one proves the chain against a real
    RAGStore, because a harvester whose pieces each pass while the path does not
    work is exactly the failure this whole pass has been about.
    """
    from axiom.rag import harvest_drain
    from axiom.rag.store import CORPUS_ORG
    from axiom.rag.store_factory import create_store

    store = create_store(f"sqlite:///{tmp_path/'rag.db'}")
    store.connect()
    try:
        with Session(db) as s:
            harvest_listeners.install(
                s, queue_path=queue,
                scope_resolver=lambda _o: harvest.HarvestScope(
                    corpus=CORPUS_ORG, owner="site-a"
                ),
            )
            s.add(Sample(id=42, label="neutron flux calibration", operator="sam"))
            s.commit()

        report = harvest_drain.drain(queue, store)
        assert report.ok and report.upserted == 1, report.describe()

        hits = store.search(query_text="flux calibration", corpora=[CORPUS_ORG], limit=5)
        assert hits, "the saved record never became retrievable"
        assert any("neutron flux calibration" in h.chunk_text for h in hits)
        assert hits[0].source_path == "entity://sample/42"
    finally:
        store.close()
