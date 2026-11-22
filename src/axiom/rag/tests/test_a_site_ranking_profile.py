# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A site declares how its documents rank, and every surface reads it (#1984, step 1).

A site's chat ranked through its own copy of the retriever, which had learned three things
Axiom's ``retrieve()`` had not: a developer file (a design document or source file that reached
the user-facing corpus) yields to a site document, a draft yields to its finished version, and a
question about what the site's governing documents permit prefers those documents. The site's
own corpus also outranks generic literature. Here those are one :class:`RankingProfile`, declared
in a file the node names, so the chat, MCP retrieve, ``axi rag search`` and the site's own chat
all rank the same way.

Nothing is on by default: with no profile, ranking is exactly what it was.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.rag import ranking
from axiom.rag.hybrid import RerankWeights
from axiom.rag.ranking import DEVELOPER_PATH_PATTERN, DRAFT_PATTERN, RankingProfile, load_profile
from axiom.rag.retriever import UNFILTERED, retrieve
from axiom.rag.store import SearchResult


def _hit(path: str, text: str, *, title: str | None = None, corpus: str = "site", idx: int = 0) -> SearchResult:
    return SearchResult(
        source_path=path,
        source_title=title if title is not None else path.rsplit("/", 1)[-1],
        chunk_text=text,
        chunk_index=idx,
        similarity=0.5,
        combined_score=0.5,
        corpus=corpus,
    )


def _store(hits: list[SearchResult]) -> MagicMock:
    """Both rankings return ``hits`` in the given order, so RRF keeps that order."""
    store = MagicMock()
    store.search.side_effect = lambda **_: list(hits)
    return store


def _paths(store, query: str, profile: RankingProfile | None = None, **kw) -> list[str]:
    out = retrieve(store, query, [0.1] * 4, limit=10, access_context=UNFILTERED, profile=profile, **kw)
    return [c.source_path for c in out]


#: The weights the site measured on its own questions (its 1.6.166 release).
SITE_WEIGHTS = RerankWeights(developer=1.0, draft=0.75, primary=2.0)
SITE = RankingProfile(
    weights=SITE_WEIGHTS,
    authority_corpus="site",
    developer_path_re=DEVELOPER_PATH_PATTERN,
    draft_re=DRAFT_PATTERN,
    primary_query_re=r"\b(charter|licen[sc]ed?|operating\s+limits?)\b",
    primary_document_re=r"\bcharter\b|operating\s+licen[sc]e",
)


class TestNothingChangesWithoutAProfile:
    def test_the_default_profile_ranks_as_before(self):
        hits = [
            _hit("/docs/spec-widget.md", "the widget power limit field"),
            _hit("/Charter/Operating Licence.pdf", "the power limit is 2 units"),
            _hit("/Reports/Draft annual report.pdf", "power limit 2 units draft"),
        ]
        before = _paths(_store(hits), "what is the power limit", rerank_weights=RerankWeights())
        assert _paths(_store(hits), "what is the power limit", profile=RankingProfile()) == before

    def test_the_new_weights_are_off_by_default(self):
        w = RerankWeights()
        assert (w.developer, w.draft, w.primary) == (0.0, 0.0, 0.0)

    def test_no_profile_file_means_the_default(self, monkeypatch):
        monkeypatch.delenv("AXIOM_RAG_RANKING", raising=False)
        assert ranking.active_profile() == RankingProfile()


class TestDocumentKind:
    def test_a_developer_file_yields_to_a_site_document(self):
        hits = [
            _hit("/repo/docs/tech-specs/spec-widget.md", "power limit field: the power limit in units"),
            _hit("/Operations/Annual report.pdf", "the power limit is 2 units"),
        ]
        assert _paths(_store(hits), "power limit", SITE)[0] == "/Operations/Annual report.pdf"

    @pytest.mark.parametrize("path", [
        "/repo/docs/adr-012-retrieval.md", "/repo/src/widget/limits.py", "/repo/Makefile",
        "/repo/packages/ui/index.ts", "/repo/.github/workflows/ci.yml",
    ])
    def test_the_generic_pattern_knows_developer_files(self, path):
        assert SITE.is_developer_file(path)

    @pytest.mark.parametrize("path", [
        "/Operations/Tech Specs (1).pdf", "/Reports/2023 Annual Report.pdf", "/Logs/console log.txt",
    ])
    def test_documents_named_in_words_are_not_developer_files(self, path):
        assert not SITE.is_developer_file(path)

    def test_a_developer_file_in_the_authority_corpus_gets_no_authority(self):
        hits = [
            _hit("/repo/docs/spec-widget.md", "power limit", corpus="site"),
            _hit("/Library/Handbook.pdf", "power limit", corpus="literature"),
        ]
        # Same text, same fused place: only the developer prior and the lost authority differ.
        assert _paths(_store(hits), "power limit", SITE)[0] == "/Library/Handbook.pdf"

    def test_a_draft_yields_to_its_finished_version(self):
        hits = [
            _hit("/Reports/Annual report DRAFT v2.pdf", "the power limit is 2 units"),
            _hit("/Reports/Annual report.pdf", "the power limit is 2 units"),
        ]
        assert _paths(_store(hits), "power limit", SITE)[0] == "/Reports/Annual report.pdf"

    def test_a_draft_named_only_in_its_title_is_a_draft(self):
        assert SITE.is_draft("/Reports/report-v2.pdf", "Annual report (draft)")
        assert not SITE.is_draft("/Reports/overdraft fees.pdf", "Overdraft fees")

    def test_a_question_about_what_is_permitted_prefers_the_governing_document(self):
        hits = [
            _hit("/Outreach/one-pager.pdf", "licensed power limit 2 units"),
            _hit("/Reports/Annual report.pdf", "the licensed power limit is 2 units"),
            _hit("/Charter/Operating Licence.pdf", "section 3: licensed power shall not exceed 2 units"),
        ]
        assert _paths(_store(hits), "what is the licensed power limit?", SITE)[0] == "/Charter/Operating Licence.pdf"

    def test_the_governing_document_is_not_lifted_for_other_questions(self):
        hits = [
            _hit("/Reports/Annual report.pdf", "the staff count was 12 people in the annual report"),
            _hit("/Charter/Operating Licence.pdf", "section 3: power shall not exceed 2 units"),
        ]
        assert _paths(_store(hits), "annual report staff count", SITE)[0] == "/Reports/Annual report.pdf"

    def test_a_developer_file_is_never_a_governing_document(self):
        assert not SITE.is_primary_document("/repo/docs/spec-charter.md", "Charter: the licence module")


class TestAuthority:
    def test_the_site_corpus_outranks_literature_on_a_tie(self):
        hits = [
            _hit("/Library/Handbook.pdf", "power limit", corpus="literature"),
            _hit("/Operations/Annual report.pdf", "power limit", corpus="site"),
        ]
        assert _paths(_store(hits), "power limit", RankingProfile(authority_corpus="site"))[0] == (
            "/Operations/Annual report.pdf"
        )

    def test_a_site_value_term_and_code_token(self):
        profile = RankingProfile(value_intent_terms=("reach",), extra_code_re=r"x-ray")
        hits = [
            _hit("/a.pdf", "the reach of the beam is long"),
            _hit("/b.pdf", "beam reach measured at 12.50 m"),
        ]
        assert _paths(_store(hits), "beam reach", profile)[0] == "/b.pdf"
        assert "xray" in profile.terms("x-ray beam")


class TestTheProfileFile:
    def test_a_site_declares_its_profile_in_toml(self, tmp_path):
        f = tmp_path / "rag-ranking.toml"
        f.write_text(
            '[ranking]\n'
            'authority_corpus = "site"\n'
            'value_intent_terms = ["worth", "reach"]\n'
            'extra_code_re = "x-ray"\n'
            'developer_paths = "default"\n'
            'drafts = "default"\n'
            'primary_query_re = "\\\\bcharter\\\\b"\n'
            'primary_document_re = "charter"\n'
            '[ranking.weights]\n'
            'developer = 1.0\n'
            'draft = 0.75\n'
            'primary = 2.0\n'
        )
        p = load_profile(f)
        assert p.authority_corpus == "site"
        assert p.value_intent_terms == ("worth", "reach")
        assert p.developer_path_re == DEVELOPER_PATH_PATTERN
        assert p.draft_re == DRAFT_PATTERN
        assert p.weights == RerankWeights(developer=1.0, draft=0.75, primary=2.0)
        assert p.wants_primary_document("what does the charter say")

    def test_every_surface_reads_the_file_the_node_names(self, tmp_path, monkeypatch):
        f = tmp_path / "rag-ranking.toml"
        f.write_text('[ranking]\nauthority_corpus = "site"\n')
        monkeypatch.setenv("AXIOM_RAG_RANKING", str(f))
        assert ranking.active_profile().authority_corpus == "site"
        hits = [
            _hit("/Library/Handbook.pdf", "power limit", corpus="literature"),
            _hit("/Operations/Annual report.pdf", "power limit", corpus="site"),
        ]
        # retrieve() with no profile argument reads the node's file.
        assert _paths(_store(hits), "power limit")[0] == "/Operations/Annual report.pdf"

    def test_an_unknown_key_is_refused(self, tmp_path):
        f = tmp_path / "rag-ranking.toml"
        f.write_text('[ranking]\nauthority_corpas = "site"\n')
        with pytest.raises(ValueError, match="authority_corpas"):
            load_profile(f)

    def test_a_bad_pattern_is_refused(self, tmp_path):
        f = tmp_path / "rag-ranking.toml"
        f.write_text('[ranking]\nprimary_document_re = "(unclosed"\n')
        with pytest.raises(ValueError, match="primary_document_re"):
            load_profile(f)

    def test_a_broken_file_on_the_node_falls_back_to_the_default(self, tmp_path, monkeypatch, caplog):
        f = tmp_path / "rag-ranking.toml"
        f.write_text('[ranking]\nweights = 3\n')
        monkeypatch.setenv("AXIOM_RAG_RANKING", str(f))
        assert ranking.active_profile() == RankingProfile()
        assert "AXIOM_RAG_RANKING" in caplog.text
