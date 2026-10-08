# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The Postgres ledger and concept graph answer exactly as the SQLite ones did (ADR-174).

Every behaviour runs twice: through the test seam (SQLite session, no server) and
against a real Postgres when one is reachable (``AXIOM_PG_TEST_URL``, or a
``postgresql`` ``AXIOM_DB_URL``). The real run also proves the Alembic migration:
the schema is brought up by ``provision_extension('memory')``, not ``create_all``.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.artifacts.pg_backend import PostgresBackend
from axiom.artifacts.registry import Artifact, ArtifactRegistry
from axiom.memory import pg_store
from axiom.memory.graph import Concept, ConceptEdge, GraphQuery, canonical_concept_id
from axiom.memory.pg_graph import PostgresConceptGraph
from axiom.memory.pg_models import Base


@pytest.fixture(params=["seam", "postgres"])
def store(request):
    """Bind the memory layer to a SQLite seam or to a real Postgres."""
    if request.param == "seam":
        engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine)

        @contextlib.contextmanager
        def provider():
            s = factory()
            try:
                yield s
            finally:
                s.close()

        pg_store.set_provider(provider)
        yield request.param
        pg_store.reset_provider()
        return
    request.getfixturevalue("real_postgres")
    yield request.param
    pg_store.reset_provider()


def _scope() -> str:
    return f"test-{uuid.uuid4().hex[:10]}"


def _art(id_, kind="fragment", name="n", created_at=1.0, data=None, **kw):
    return Artifact(
        id=id_,
        kind=kind,
        name=name,
        data=data or {},
        content_hash="h" + id_,
        created_at=created_at,
        **kw,
    )


# ----------------------------------------------------------------------------- ledger


def test_roundtrip_including_binary_signature_and_metadata(store):
    b = PostgresBackend(_scope())
    b.put(_art("a", data={"x": [1, 2, {"y": None}]}, signature=b"\x00\xffsig", metadata={"k": "v"}))
    got = b.get("a")
    assert got.data == {"x": [1, 2, {"y": None}]}
    assert got.signature == b"\x00\xffsig"
    assert got.metadata == {"k": "v"}
    assert got.deleted is False and got.created_at == 1.0
    assert b.get("missing") is None


def test_put_again_replaces_and_does_not_duplicate(store):
    b = PostgresBackend(_scope())
    b.put(_art("a", data={"v": 1}))
    b.put(_art("a", data={"v": 2}))
    assert [x.data for x in b.list_all()] == [{"v": 2}]


def test_listing_is_oldest_first_and_ties_keep_insertion_order(store):
    b = PostgresBackend(_scope())
    b.put(_art("late", created_at=5.0))
    for i in ("t1", "t2", "t3"):
        b.put(_art(i, created_at=2.0))
    assert [a.id for a in b.list_all()] == ["t1", "t2", "t3", "late"]


def test_find_by_name_and_kind_filters_and_hides_deleted_by_default(store):
    b = PostgresBackend(_scope())
    b.put(_art("1", kind="doc", name="x", created_at=1.0))
    b.put(_art("2", kind="doc", name="x", created_at=2.0))
    b.put(_art("3", kind="note", name="x"))
    b.mark_deleted("1", "superseded")
    assert [a.id for a in b.find_by_name("doc", "x")] == ["2"]
    assert [a.id for a in b.find_by_name("doc", "x", include_deleted=True)] == ["1", "2"]
    assert [a.id for a in b.list_all(kind="note")] == ["3"]
    assert b.get("1").deleted and b.get("1").deletion_reason == "superseded"


def test_deleting_a_missing_artifact_raises_like_the_other_backends(store):
    with pytest.raises(KeyError):
        PostgresBackend(_scope()).mark_deleted("nope", "r")


def test_scopes_never_see_each_other(store):
    a, b = PostgresBackend(_scope()), PostgresBackend(_scope())
    a.put(_art("same"))
    assert b.get("same") is None and b.list_all() == []
    b.put(_art("same", data={"other": True}))
    assert a.get("same").data == {}


def _fragment(id_, ctype="episodic", principal="@ben", content=None, created_at=1.0):
    return _art(
        id_,
        data={
            "cognitive_type": ctype,
            "provenance": {"principal_id": principal},
            "content": content or {},
        },
        created_at=created_at,
    )


def test_find_fragments_filters_by_type_principal_and_nested_scope(store):
    b = PostgresBackend(_scope())
    b.put(_fragment("1", content={"classroom_id": "c1"}))
    b.put(_fragment("2", ctype="semantic", content={"classroom_id": "c1"}))
    b.put(_fragment("3", principal="@zed", content={"classroom_id": "c2"}))
    b.put(_fragment("4", content={"meta": {"room": "r9"}}))
    b.put(_art("5", kind="note"))
    assert {a.id for a in b.find_fragments()} == {"1", "2", "3", "4"}
    assert {a.id for a in b.find_fragments(cognitive_type="episodic")} == {"1", "3", "4"}
    assert {a.id for a in b.find_fragments(principal_id="@zed")} == {"3"}
    assert {a.id for a in b.find_fragments(scope_path="classroom_id", scope_value="c1")} == {
        "1",
        "2",
    }
    assert {a.id for a in b.find_fragments(scope_path="meta.room", scope_value="r9")} == {"4"}


def test_find_fragments_event_time_ordering_mixes_iso_numbers_and_missing(store):
    b = PostgresBackend(_scope())
    b.put(_fragment("iso", content={"event_time": "2026-10-06T00:00:00+00:00"}, created_at=10.0))
    b.put(_fragment("num", content={"event_time": 1.0}, created_at=999.0))
    b.put(_fragment("none", created_at=50.0))  # no event time: falls back to when it was recorded
    got = [a.id for a in b.find_fragments(order_by_event_time_desc=True)]
    assert got == ["iso", "none", "num"]
    assert [a.id for a in b.find_fragments(order_by_event_time_desc=True, limit=2)] == [
        "iso",
        "none",
    ]


def test_find_fragments_scope_path_is_a_parameter_not_sql(store):
    b = PostgresBackend(_scope())
    b.put(_fragment("1", content={"a": "x"}))
    # A hostile path must match nothing and break nothing.
    assert b.find_fragments(scope_path="a') OR 1=1 --", scope_value="x") == []
    assert len(b.find_fragments()) == 1


def test_registry_on_top_of_the_backend_versions_and_hashes(store):
    reg = ArtifactRegistry(backend=PostgresBackend(_scope()))
    id1 = reg.register(kind="doc", name="spec", data={"v": 1})
    id2 = reg.register(kind="doc", name="spec", data={"v": 2})
    assert reg.latest(kind="doc", name="spec").id == id2
    assert [x.id for x in reg.version_chain(kind="doc", name="spec")] == [id1, id2]
    assert reg.get(id1).content_hash and reg.get(id1).content_hash != reg.get(id2).content_hash


# ----------------------------------------------------------------------------- graph


def _concept(name, sources=(), confidence=1.0):
    return Concept(canonical_concept_id(name), name, list(sources), confidence)


def test_concept_merge_keeps_name_max_confidence_and_all_provenance(store):
    g = PostgresConceptGraph(_scope())
    cid = canonical_concept_id("alpha")
    g.upsert_concept(Concept(cid, "alpha", ["f1"], 0.4))
    g.upsert_concept(Concept(cid, "ALPHA RENAMED", ["f2", "f1"], 0.9))
    g.upsert_concept(Concept(cid, "alpha", ["f3"], 0.1))
    got = g.get_concept(cid)
    assert got.canonical_name == "alpha"  # immutable after first insert
    assert got.confidence == 0.9  # max ever seen
    assert got.extracted_from == ["f1", "f2", "f3"]
    assert g.concept_count() == 1


def test_edges_keep_max_weight_and_evidence(store):
    g = PostgresConceptGraph(_scope())
    a, b = _concept("a"), _concept("b")
    g.upsert_concept(a)
    g.upsert_concept(b)
    g.upsert_edge(ConceptEdge(a.concept_id, b.concept_id, "rel", 0.3, ["f1"]))
    g.upsert_edge(ConceptEdge(a.concept_id, b.concept_id, "rel", 0.8, ["f2"]))
    g.upsert_edge(ConceptEdge(a.concept_id, b.concept_id, "rel", 0.1, ["f1"]))
    assert g.edge_count() == 1


def test_neighbors_walk_both_directions_by_hops_and_edge_type(store):
    g = PostgresConceptGraph(_scope())
    n = {x: _concept(x) for x in "abcd"}
    for c in n.values():
        g.upsert_concept(c)
    g.upsert_edge(ConceptEdge(n["a"].concept_id, n["b"].concept_id, "rel"))
    g.upsert_edge(ConceptEdge(n["c"].concept_id, n["b"].concept_id, "rel"))  # incoming to b
    g.upsert_edge(ConceptEdge(n["c"].concept_id, n["d"].concept_id, "other"))
    one = {c.canonical_name for c in g.neighbors(n["a"].concept_id, hops=1)}
    two = {c.canonical_name for c in g.neighbors(n["a"].concept_id, hops=2)}
    three = {c.canonical_name for c in g.neighbors(n["a"].concept_id, hops=3)}
    typed = {
        c.canonical_name
        for c in g.neighbors(n["a"].concept_id, hops=3, edge_types=frozenset({"rel"}))
    }
    assert one == {"b"} and two == {"b", "c"} and three == {"b", "c", "d"} and typed == {"b", "c"}
    assert g.neighbors(n["a"].concept_id, hops=0) == []
    # BFS order is preserved: nearer concepts come first.
    assert [c.canonical_name for c in g.neighbors(n["a"].concept_id, hops=3)][0] == "b"


def test_query_respects_limit_and_all_concepts_is_name_ordered(store):
    g = PostgresConceptGraph(_scope())
    hub = _concept("hub")
    g.upsert_concept(hub)
    for name in ("zeta", "alpha", "mid"):
        c = _concept(name)
        g.upsert_concept(c)
        g.upsert_edge(ConceptEdge(hub.concept_id, c.concept_id, "rel"))
    assert [c.canonical_name for c in g.all_concepts()] == ["alpha", "hub", "mid", "zeta"]
    got = g.query(GraphQuery(seed_concepts=frozenset({hub.concept_id}), limit=2))
    assert len(got) == 2


def test_graph_scopes_are_isolated(store):
    g1, g2 = PostgresConceptGraph(_scope()), PostgresConceptGraph(_scope())
    g1.upsert_concept(_concept("only-here"))
    assert g2.concept_count() == 0 and g2.get_concept(canonical_concept_id("only-here")) is None


def test_an_unconfigured_database_says_what_to_do_not_a_stack_trace(monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql+psycopg2://nobody:x@127.0.0.1:1/none")
    pg_store.reset_provider()
    try:
        with pytest.raises(pg_store.MemoryStoreUnavailable, match="ADR-174"):
            pg_store.ensure_provisioned()
    finally:
        pg_store.reset_provider()


def test_the_migration_builds_its_tables_in_its_own_schema_never_public(real_postgres):
    """ADR-052: an extension writes only to its schema. Search-path fallback hides a miss
    (the ORM finds a table in public through "memory, public"), so look at the catalog."""
    engine = create_engine(real_postgres)
    pg_store.ensure_provisioned()
    names = (
        "artifact",
        "concept",
        "concept_extracted_from",
        "concept_edge",
        "concept_edge_evidence",
    )
    with engine.connect() as c:
        rows = c.execute(
            text(
                "select table_schema, table_name from information_schema.tables where table_name = any(:n)"
            ),
            {"n": list(names)},
        ).fetchall()
    from axiom.infra.db import normalize_extension_name

    schema = normalize_extension_name("memory")  # carries the per-worker test suffix, if any
    found = {(sc, n) for sc, n in rows}
    assert {(schema, n) for n in names} <= found
    assert not [r for r in found if r[0] == "public"], f"tables leaked into public: {found}"
