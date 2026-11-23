# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit LD-2: the search index and RAG corpus from gold, pre-filtered (#1759,
ADR-251 D1, D3, D4 and D5).

The log's records are projected to gold (``gold.log_records``, :mod:`attest.gold`); the index
(:mod:`attest.index`) is derived from that view only: one chunk per signed record, keyed by record
id and the embedder's ``(model, dim)``, carrying the record's labels. One hybrid, time-first search
reads it: the one check decides which logs the caller may read, the database is asked only about
those (the pre-filter), and full text and semantic candidates are fused by reciprocal rank fusion.
The Timeline, the Log page's search and chat call that one search.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
from dataclasses import replace
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import doors as log_doors  # noqa: E402
from axiom.extensions.builtins.attest import gold, index, registry, service  # noqa: E402
from axiom.extensions.builtins.attest.skills import bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import _sign, person  # noqa: E402
from axiom.extensions.builtins.journal import rights  # noqa: E402
from axiom.governance import derived_sources  # noqa: E402
from axiom.governance import doors as g  # noqa: E402
from axiom.infra import audit_trail  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skill_dispatch import invoke_capability  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402

SITE = "site-a"
LOG = "demo_log"
HIDDEN = "ld2_hidden_log"
ME = "@op1:site-a"
OTHER = "@op2:site-a"
ROOT = Path(__file__).resolve().parents[2]
#: A credential-shaped string (the platform's ``github-token`` format); not a real token.
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _draft(logbook: str, title: str, note: str = "", **fields):
    return service.create_draft(
        site_id=SITE, logbook=logbook, entry_type="ROUND_CHECK", meaning="performed",
        content={"title": title, "fields": {"reading": "ok", "walkdown": True,
                                             **({"note": note} if note else {}), **fields}},
        origin="human", for_principal=ME,
    )  # fmt: skip


def sign(signer, title: str, note: str = "", logbook: str = LOG) -> dict:
    return _sign(_draft(logbook, title, note), signer).record


@pytest.fixture
def world(client, attest_db, node_signer, tmp_path, monkeypatch):
    """Two readable records in the demo log and one in a log labelled above the caller."""
    from axiom.extensions.builtins.attest import signing

    audit_trail._reset_for_tests()
    rights.forget_decisions()
    signing.set_provider(lambda: node_signer)
    monkeypatch.setenv("AXIOM_SITE", SITE)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    import axiom.infra.paths as paths

    monkeypatch.setattr(paths, "get_user_state_dir", lambda: tmp_path)
    index.set_registry(None)  # the built-in embedder: deterministic, on this machine
    registry.register(replace(registry.get(LOG), id=HIDDEN, handle=None, classification="regulated"))
    pump = sign(node_signer, "Pump seal noted", "pump seal weeping at the gland")
    valve = sign(node_signer, "Valve lineup", "valve lineup checked before the pump start")
    yield {"client": client, "pump": pump, "valve": valve, "state": tmp_path, "signer": node_signer}
    gold.drop_for_tests()
    audit_trail._reset_for_tests()


def me(state: Path | None = None, **kw) -> log_doors.LogCaller:
    return log_doors.LogCaller(handle=ME, sites=frozenset({SITE}), state_dir=state, **kw)


def ids(result: dict) -> list[str]:
    return [r["attestation_id"] for r in result["results"]]


def chunks() -> list[dict]:
    return index.rows_for_tests()


# -- LD-2-A1: the indexer reads gold only ---------------------------------------------------


def _indexer_modules() -> list[Path]:
    """Every indexer, corpus and embedding job in the platform: by name, or by declaring its
    sources."""
    found = []
    for path in sorted((ROOT / "src" / "axiom").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel:
            continue
        text = path.read_text(encoding="utf-8")
        if derived_sources.is_indexer(rel, text):
            found.append(path)
    return found


@pytest.mark.acceptance("LD-2-A1")
def test_ld2_a1_the_indexers_declared_sources_are_gold_objects_only():
    assert index.INDEX_SOURCES and all(s.startswith("gold.") for s in index.INDEX_SOURCES)
    assert derived_sources.declared_sources_ok(index.INDEX_SOURCES) == []
    modules = _indexer_modules()
    assert ROOT / "src/axiom/extensions/builtins/attest/index.py" in modules
    bad = {p.relative_to(ROOT).as_posix(): derived_sources.violations(p.read_text(encoding="utf-8"))
           for p in modules}  # fmt: skip
    assert {k: v for k, v in bad.items() if v} == {}
    # The log's index never names the chain's own table: it reads the gold view of it.
    source = (ROOT / "src/axiom/extensions/builtins/attest/index.py").read_text(encoding="utf-8")
    assert "attest_records" not in source and "AttestRecord" not in source


@pytest.mark.acceptance("LD-2-A1")
def test_ld2_a1_the_guard_fails_on_silver_a_drafts_table_or_a_tray():
    planted = (
        'INDEX_SOURCES = ("gold.log_records", "silver.signals")\n'
        'SQL = "SELECT * FROM silver.signals JOIN attest_drafts USING (site_id)"\n'
        "from x import AttestDraft\n"
        "def job(person):\n    return person.tray()\n"
    )
    found = derived_sources.violations(planted)
    assert {f.split(":")[0] for f in found} >= {"silver", "drafts", "tray"}, found
    assert derived_sources.declared_sources_ok(("gold.log_records", "silver.signals")) == ["silver.signals"]
    assert derived_sources.is_indexer("src/axiom/x/corpus_job.py", "")
    assert derived_sources.is_indexer("src/axiom/x/anything.py", planted)


# -- LD-2-A2: one signed entry is one chunk -------------------------------------------------


@pytest.mark.acceptance("LD-2-A2")
def test_ld2_a2_one_entry_one_chunk_with_its_labels_and_its_embedder(world, monkeypatch):
    monkeypatch.setenv("AXIOM_FLAG_LOG_ACKNOWLEDGEMENTS", "on")
    pump = world["pump"]
    d = service.create_supplement_draft(site_id=SITE, target=pump["attestation_id"], reason="addition",
                                        content={"body": "gland repacked afterwards"}, origin="human",
                                        for_principal=ME)  # fmt: skip
    supp = _sign(d, world["signer"]).record
    ack = _sign(service.create_ack_draft(site_id=SITE, target=pump["attestation_id"], origin="human",
                                         for_principal=OTHER),
                world["signer"], who=person(handle=OTHER)).record  # fmt: skip
    hidden = sign(world["signer"], "Hidden pump note", logbook=HIDDEN)
    index.refresh()
    rows = {r["attestation_id"]: r for r in chunks()}
    chain = [pump, world["valve"], supp, ack, hidden]
    assert set(rows) == {r["attestation_id"] for r in chain}  # one chunk per record, no more
    space = index.registry().canonical
    for rec in chain:
        row = rows[rec["attestation_id"]]
        book = registry.get(rec["logbook"])
        assert (row["model"], row["dim"]) == (space.model, space.dim)
        assert (row["site_id"], row["logbook"], row["seq"]) == (SITE, rec["logbook"], rec["seq"])
        assert row["classification"] == book.classification
        assert row["export_controlled"] is book.export_controlled
        assert row["roles"] == index.reader_roles(book)
        assert row["kind"] == rec.get("kind", "statement")
        assert len(row["embedding"]) == space.dim
    assert rows[supp["attestation_id"]]["target"] == pump["attestation_id"]
    assert rows[supp["attestation_id"]]["kind"] == "supplement"
    assert rows[ack["attestation_id"]]["target"] == pump["attestation_id"]
    assert rows[ack["attestation_id"]]["kind"] == "ack"
    # Nothing is concatenated: the supplement's chunk holds its own words, not its target's.
    lexemes = index.lexemes_for_tests(supp["attestation_id"])
    assert "repacked" in lexemes and "weeping" not in lexemes
    assert "repacked" not in index.lexemes_for_tests(pump["attestation_id"])


@pytest.mark.acceptance("LD-2-A2")
def test_ld2_a2_a_record_without_labels_or_without_an_embedder_is_not_indexed(world, monkeypatch):
    monkeypatch.delitem(registry._logbooks, LOG)  # the declaration, and so the labels, are gone
    index.refresh()
    assert chunks() == []
    with pytest.raises(index.IndexRefused, match="labels"):
        index.chunk_for({"attestation_id": "x", "logbook": LOG}, labels=None, roles=())
    with pytest.raises(index.IndexRefused, match="embedder"):
        index.chunk_for({"attestation_id": "x", "logbook": LOG},
                        labels={"classification": "internal", "export_controlled": False},
                        roles=(), space=None)  # fmt: skip


# -- LD-2-A3: rights and filters run before ranking -----------------------------------------


@pytest.mark.acceptance("LD-2-A3")
def test_ld2_a3_the_ranker_never_receives_a_row_the_caller_may_not_read(world, monkeypatch):
    before = index.search(me(world["state"]), "pump seal")
    hidden = sign(world["signer"], "Pump seal pump seal pump seal", "pump seal", logbook=HIDDEN)
    seen: list[str] = []
    real = index._rank

    def instrumented(text_hits, vector_hits, qvec, limit):
        seen.extend(c.attestation_id for c in (*text_hits, *vector_hits))
        return real(text_hits, vector_hits, qvec, limit)

    monkeypatch.setattr(index, "_rank", instrumented)
    after = index.search(me(world["state"]), "pump seal")
    assert seen and hidden["attestation_id"] not in seen
    # A denied row changes neither the rank, the score nor the count of what is returned.
    strip = lambda r: [(x["attestation_id"], x["score"], x["rank"]) for x in r["results"]]  # noqa: E731
    assert strip(after) == strip(before) and after["count"] == before["count"]
    assert ids(after)[0] == world["pump"]["attestation_id"]
    # The row would have ranked first for a caller cleared to read it: it was withheld, not missing.
    seen.clear()
    cleared = index.search(me(world["state"], ceiling="regulated"), "pump seal")
    assert ids(cleared)[0] == hidden["attestation_id"] and hidden["attestation_id"] in seen


@pytest.mark.acceptance("LD-2-A3")
def test_ld2_a3_structured_filters_run_in_the_database_before_ranking(world, monkeypatch):
    seen: list[str] = []
    real = index._rank

    def instrumented(text_hits, vector_hits, qvec, limit):
        seen.extend(c.attestation_id for c in (*text_hits, *vector_hits))
        return real(text_hits, vector_hits, qvec, limit)

    monkeypatch.setattr(index, "_rank", instrumented)
    valve = world["valve"]
    got = index.search(me(world["state"]), "pump", until=world["pump"]["occurred_at"])
    assert valve["attestation_id"] not in seen and ids(got) == [world["pump"]["attestation_id"]]
    seen.clear()
    assert index.search(me(world["state"]), "pump", signer="@nobody:site-a")["results"] == []
    assert seen == []


@pytest.mark.acceptance("LD-2-A3")
def test_ld2_a3_a_refused_caller_or_policy_outage_refuses_the_whole_search(world, monkeypatch):
    from axiom.infra import site_authority

    with pytest.raises(index.SearchRefused) as exc:
        index.search(log_doors.LogCaller(handle=None), "pump")
    assert exc.value.decision.reason is g.Reason.UNRESOLVED_CALLER

    def unreachable(*_a, **_k):
        raise ConnectionError("GUARD is unreachable")

    with monkeypatch.context() as m:
        m.setattr(site_authority, "decide_site_read", unreachable)
        with pytest.raises(index.SearchRefused) as exc:
            index.search(me(world["state"]), "pump")
    assert exc.value.decision.reason is g.Reason.POLICY_UNAVAILABLE
    # A site outside the caller's scope is answered as a site with nothing in it.
    outside = log_doors.LogCaller(handle="@op9:site-b", sites=frozenset({"site-b"}))
    assert index.search(outside, "pump", site=SITE)["results"] == []


# -- LD-2-A4: as of, and a deep link ---------------------------------------------------------


@pytest.mark.acceptance("LD-2-A4")
def test_ld2_a4_every_result_is_as_of_the_gold_watermark_and_deep_linked(world):
    result = index.search(me(world["state"]), "pump")
    newest = max(world["pump"]["recorded_at"], world["valve"]["recorded_at"])
    assert result["as_of"] == gold.watermark(SITE, LOG)
    assert gold.watermark(SITE, LOG) is not None
    assert gold.watermark(SITE, LOG)[:19] == str(newest)[:19]
    c = as_(world["client"], OP1)
    assert result["results"]
    for r in result["results"]:
        assert r["as_of"] == result["as_of"]
        aid = r["attestation_id"]
        assert r["link"] == f"/api/v1/attest/{LOG}/records/{aid}?site={SITE}"
        assert r["url"] == f"#/log/{LOG}/{aid}"
        got = c.get(r["link"])  # through the attest door, with its own check
        assert got.status_code == 200, got.text
        assert got.json()["record"]["attestation_id"] == aid


# -- LD-2-A5: one skill, three callers -------------------------------------------------------


def _ctx(state: Path, handle: str) -> SkillContext:
    principal = PrincipalContext(handle=handle, posture="sso", assured=True)
    return SkillContext(registry=bind_default(), state_dir=state, logger=logging.getLogger("ld2"),
                        principal=principal, actor=handle, surface="chat")  # fmt: skip


@pytest.mark.acceptance("LD-2-A5")
def test_ld2_a5_the_timeline_the_log_page_and_chat_get_the_same_rows_in_the_same_order(
    world, monkeypatch
):
    from axiom.extensions.builtins.journal import api as journal_api
    from axiom.extensions.builtins.search import api as search_api

    monkeypatch.setenv(journal_api.INGEST_ENV, "0")
    router = fastapi.APIRouter(prefix="/api/v1")
    search_api.register_routes(router)
    journal_api.register_routes(router)
    c = world["client"]
    c.app.include_router(router)
    sign(world["signer"], "Pump start", "pump started after the valve lineup")
    query = "pump valve"
    as_(c, OP1)
    timeline = c.get("/api/v1/journal/search", params={"q": query, "logbook": LOG})
    assert timeline.status_code == 200, timeline.text
    log_page = c.get("/api/v1/search", params={"q": query, "kinds": "logbook", "logbook": LOG})
    assert log_page.status_code == 200, log_page.text
    ctx = _ctx(world["state"], ME)
    chat = invoke_capability(ctx.registry, "attest.search", {"query": query, "logbook": LOG,
                                                             "site": SITE}, ctx, surface="agent_tool")  # fmt: skip
    assert chat.ok, chat.errors
    from_timeline = ids(timeline.json())
    from_log_page = [h["id"] for h in log_page.json()["results"]]
    from_chat = ids(chat.value)
    assert len(from_timeline) >= 2
    assert from_timeline == from_log_page == from_chat


@pytest.mark.acceptance("LD-2-A5")
def test_ld2_a5_no_surface_holds_a_private_index():
    """The index is named in one module; every surface calls its search."""
    holders = set()
    for path in (ROOT / "src" / "axiom").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel:
            continue
        text = path.read_text(encoding="utf-8")
        if index.INDEX_TABLE in text:
            holders.add(rel)
    assert holders == {"src/axiom/extensions/builtins/attest/index.py"}
    callers = set()
    for path in (ROOT / "src" / "axiom").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "search" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "log_index"):  # fmt: skip
                callers.add(rel)
    assert callers == {
        "src/axiom/extensions/builtins/attest/skills/search.py",  # chat, MCP and the CLI
        "src/axiom/extensions/builtins/journal/api.py",  # the Timeline
        "src/axiom/extensions/builtins/search/engine.py",  # the Log page's search, the palette
    }
    for door_id in ("attest.search", "journal.search", "search.logbooks"):
        assert g.get(door_id).shape == "listing"


# -- LD-2-A6: secrets are redacted in the chunk, never in the record -------------------------


@pytest.mark.acceptance("LD-2-A6")
def test_ld2_a6_a_secret_is_stored_redacted_and_an_exposure_is_recorded(world):
    leaky = sign(world["signer"], "Credential pasted", f"token {TOKEN} left in the note")
    index.refresh(state_dir=world["state"])
    row = next(r for r in chunks() if r["attestation_id"] == leaky["attestation_id"])
    assert row["redacted"] is True and row["secret_patterns"] == ["github-token"]
    lexemes = index.lexemes_for_tests(leaky["attestation_id"])
    assert not any(TOKEN.lower()[:12] in lx for lx in lexemes) and "credential" in lexemes
    assert TOKEN not in json.dumps(chunks(), default=str)
    exposures = (world["state"] / "secrets" / "exposures.jsonl").read_text().splitlines()
    events = [json.loads(e) for e in exposures]
    assert any(e["event"] == "exposure" and leaky["attestation_id"] in e["where"]
               and e["pattern"] == "github-token" for e in events)  # fmt: skip
    assert TOKEN not in "\n".join(exposures)
    # A search shows the redacted words, never the secret.
    found = index.search(me(world["state"]), "credential pasted")
    hit = next(r for r in found["results"] if r["attestation_id"] == leaky["attestation_id"])
    assert TOKEN not in json.dumps(hit) and "[redacted:github-token]" in hit["snippet"]
    # The record itself is untouched.
    assert TOKEN in json.dumps(service.record(leaky["attestation_id"]))
    signer = world["signer"]
    assert service.verify_logbook(SITE, LOG, {signer.key_id: signer.public_bytes}).ok


# -- LD-2-A7: an imported entry is labelled import and can be filtered out -------------------


@pytest.mark.acceptance("LD-2-A7")
def test_ld2_a7_an_imported_entry_is_embedded_as_import_and_can_be_filtered_out(world, tmp_path):
    from datetime import date, timedelta

    from axiom.extensions.builtins.attest import imports, roles

    roles.grant("@bo:site-a", SITE, "importer", by="@admin:site-a", state_dir=world["state"],
                logbook=LOG, until=(date.today() + timedelta(days=30)).isoformat())  # fmt: skip
    content = (json.dumps({"occurred_at": "2025-01-01T08:00:00Z", "title": "Pump seal history",
                           "source_ref": "paper-1", "by": "J.D."}) + "\n").encode()  # fmt: skip
    dry = imports.dry_run(SITE, LOG, content)
    done = imports.commit(SITE, LOG, content, digest=dry["digest"], by="@bo:site-a",
                          state_dir=world["state"])  # fmt: skip
    imported = done["records"][0]["attestation_id"]
    index.refresh()
    row = next(r for r in chunks() if r["attestation_id"] == imported)
    assert row["kind"] == "import"
    both = index.search(me(world["state"]), "pump seal")
    assert imported in ids(both)
    assert next(r for r in both["results"] if r["attestation_id"] == imported)["kind"] == "import"
    signed_only = index.search(me(world["state"]), "pump seal", exclude_kinds=["import"])
    assert imported not in ids(signed_only) and world["pump"]["attestation_id"] in ids(signed_only)


# -- LD-2-A8: drop and rebuild gives the same rows -------------------------------------------


def _chain_digest() -> str:
    signer_recs = service.records(SITE, LOG)
    return hashlib.sha256("".join(r["digest"] for r in signer_recs).encode()).hexdigest()


@pytest.mark.acceptance("LD-2-A8")
def test_ld2_a8_drop_and_rebuild_yields_identical_rows_and_loses_no_official_copy(world):
    sign(world["signer"], "Credential pasted", f"token {TOKEN}")
    index.refresh(state_dir=world["state"])
    first = index.digest()
    chain_before = _chain_digest()
    assert len(chunks()) == 3
    index.drop()
    assert index.rows_for_tests() == []
    assert _chain_digest() == chain_before  # nothing official lived in the index
    index.rebuild(state_dir=world["state"])
    assert index.digest() == first and len(chunks()) == 3
    assert re.fullmatch(r"[0-9a-f]{64}", first)


# -- Must refuse: a transport that bypasses the check ----------------------------------------


@pytest.mark.acceptance("LD-2-A3")
def test_ld2_the_generic_gold_verbs_answer_the_logs_gold_objects_as_missing(world, monkeypatch):
    """The log's gold view and its index carry record content and labels; they are served only
    through the log's doors. ``data.describe``, ``data.table`` and the other generic gold verbs
    answer them as a table that does not exist (no existence hint), and do not list them."""
    from axiom.extensions.builtins.data_platform import gold_query as gq
    from axiom.extensions.builtins.data_platform import table_door as td

    index.refresh()
    with service.store.session_scope() as s:
        cur = s.connection().connection.cursor()
        for obj in (gold.VIEW, index.INDEX_TABLE, index.STATE_TABLE):
            with pytest.raises(gq.UnknownObject):
                gq._columns_of(cur, obj, gold.schema())
        cur.execute(gq._TABLES_SQL, [gold.schema()])
        assert {r[0] for r in cur.fetchall()} & {gold.VIEW, index.INDEX_TABLE, index.STATE_TABLE} == set()
        monkeypatch.setattr(td, "GOLD", gold.schema())
        for obj in (gold.VIEW, index.INDEX_TABLE):
            with pytest.raises(gq.UnknownObject):
                td.load_relation(cur, obj)
        # A gold object nobody marked is still served: the mark, not the schema, decides.
        cur.execute(f'CREATE VIEW "{gold.schema()}".ld2_plain AS SELECT 1 AS x')
        assert gq._columns_of(cur, "ld2_plain", gold.schema()) == [("x", "integer")]


@pytest.mark.acceptance("LD-2-A1")
@pytest.mark.parametrize("hostile", [
    'gold"; DROP TABLE x; --', "gold.log_records", "1gold", "gold space", "", "g" * 64,
    "gold\x00", "gölď", None,
])  # fmt: skip
def test_ld2_a_hostile_schema_or_table_name_is_refused_before_any_statement(hostile, monkeypatch):
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        gold.ident(hostile)
    # The gold schema and the index's tables pass the same helper: a hostile suffix never reaches SQL.
    if isinstance(hostile, str) and hostile:
        monkeypatch.setenv("AXIOM_TEST_SCHEMA_SUFFIX", "")
        monkeypatch.setattr("axiom.infra.db.normalize_extension_name", lambda _n: hostile)
        with pytest.raises(ValueError, match="unsafe SQL identifier"):
            gold.qualified()
        with pytest.raises(ValueError, match="unsafe SQL identifier"):
            index._t(index.INDEX_TABLE)
    assert gold.ident("gold_t1a2b") == '"gold_t1a2b"' and gold.ident("g" * 63) == '"' + "g" * 63 + '"'
