# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the rag-memory recall projection (ADR-088).

Per-type policy: core/semantic/episodic/procedural/resource project by
default; vault is categorically excluded — no argument, configuration,
or direct call can include it. Chunks carry cognitive_type,
fragment_ref, visibility, classification; resource projects as
metadata + pointer, never blob content. The projection is pure
(read-side only) and rebuildable."""

from __future__ import annotations

import inspect

import pytest


def _frag(ctype: str, content: dict, principal: str = "@alice:p"):
    from axiom.memory.fragment import create_fragment

    return create_fragment(
        content=content, cognitive_type=ctype,
        principal_id=principal, agents={"axi"}, resources=set(),
    )


_SAMPLES = {
    "core": {"persona": "prefers concise answers"},
    "semantic": {"fact": "prefers TDD"},
    "episodic": {"summary": "sprint recap", "event_time": "2026-07-01T09:00:00+00:00"},
    "procedural": {"steps": ["a", "b"], "summary": "how to deploy"},
    "resource": {"ref": "box://folder/report.pdf", "name": "report",
                 "blob": "SHOULD-NEVER-RENDER"},
}


class TestPerTypePolicy:
    @pytest.mark.parametrize("ctype", list(_SAMPLES))
    def test_five_types_project(self, ctype):
        from axiom.memory.recall_projection import fragment_to_recall_chunk

        chunk = fragment_to_recall_chunk(_frag(ctype, _SAMPLES[ctype]))
        assert chunk.cognitive_type == ctype
        assert chunk.chunk_text.strip()

    def test_vault_raises_on_direct_call(self):
        from axiom.memory.recall_projection import fragment_to_recall_chunk

        with pytest.raises(ValueError, match="vault"):
            fragment_to_recall_chunk(_frag("vault", {"secret": "K=1"}))

    def test_vault_skipped_in_bulk(self):
        from axiom.memory.recall_projection import project_for_recall

        frags = [_frag("semantic", {"fact": "x"}), _frag("vault", {"secret": "K=1"})]
        chunks = project_for_recall(frags)
        assert [c.cognitive_type for c in chunks] == ["semantic"]

    def test_vault_exclusion_not_configurable(self):
        """No public surface accepts a type policy: the exclusion is by
        construction, not configuration."""
        from axiom.memory import recall_projection as rp

        assert "vault" not in {t.value for t in rp.RECALL_PROJECTABLE}
        assert isinstance(rp.RECALL_PROJECTABLE, frozenset)
        for fn in (rp.fragment_to_recall_chunk, rp.project_for_recall):
            params = inspect.signature(fn).parameters
            assert not any(
                "type" in name or "polic" in name or "include" in name
                for name in params
            ), f"{fn.__name__} must not accept a type policy"


class TestChunkContract:
    def test_contract_fields(self):
        from axiom.memory.recall_projection import fragment_to_recall_chunk

        frag = _frag("semantic", {"fact": "prefers TDD"})
        chunk = fragment_to_recall_chunk(frag)
        assert chunk.fragment_ref == frag.id
        assert chunk.principal_id == "@alice:p"
        assert chunk.visibility == frag.visibility.value
        assert chunk.classification == frag.classification.to_dict()
        assert chunk.source_path == f"memory://{frag.id}"

    def test_corpus_name_convention(self):
        from axiom.memory.recall_projection import (
            fragment_to_recall_chunk,
            recall_corpus_for,
        )

        assert recall_corpus_for("@alice:p") == "rag-memory:@alice:p"
        chunk = fragment_to_recall_chunk(_frag("semantic", {"fact": "x"}))
        assert chunk.corpus == "rag-memory:@alice:p"

    def test_recall_corpora_never_in_document_defaults(self):
        from axiom.rag.store import ALL_CORPORA

        assert not any(c.startswith("rag-memory") for c in ALL_CORPORA)

    def test_resource_projects_pointer_not_blob(self):
        from axiom.memory.recall_projection import fragment_to_recall_chunk

        chunk = fragment_to_recall_chunk(_frag("resource", _SAMPLES["resource"]))
        assert "box://folder/report.pdf" in chunk.chunk_text
        assert "SHOULD-NEVER-RENDER" not in chunk.chunk_text

    def test_episodic_carries_event_time(self):
        from axiom.memory.recall_projection import fragment_to_recall_chunk

        chunk = fragment_to_recall_chunk(_frag("episodic", _SAMPLES["episodic"]))
        assert chunk.event_time == "2026-07-01T09:00:00+00:00"

    def test_to_rag_chunk_mapping(self):
        from axiom.memory.recall_projection import fragment_to_recall_chunk

        frag = _frag("semantic", {"fact": "prefers TDD"})
        rc = fragment_to_recall_chunk(frag)
        chunk = rc.to_rag_chunk()
        assert chunk.text == rc.chunk_text
        assert chunk.source_path == f"memory://{frag.id}"
        assert chunk.source_type == "memory"
