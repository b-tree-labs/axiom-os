# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Memory → RAG projection (ADR-069 feeder ii) — policy + transform + floors."""

from __future__ import annotations

import pytest

from axiom.memory.fragment import create_fragment
from axiom.memory.rag_projection import (
    ProjectedChunk,
    fragment_to_chunk,
    project_fragments,
    should_project,
)


def _frag(cognitive_type: str, content: dict):
    return create_fragment(
        content=content,
        cognitive_type=cognitive_type,
        principal_id="@ben",
        agents={"@axi"},
        resources=set(),
    )


# --- which types project ----------------------------------------------------#
def test_semantic_projects():
    assert should_project(_frag("semantic", {"summary": "Postgres DSNs go in env"})) is True


def test_vault_never_projects():
    assert should_project(_frag("vault", {"secret": "x"})) is False


def test_core_never_projects():
    assert should_project(_frag("core", {"identity": "axiom"})) is False


def test_raw_episodic_never_projects():
    assert should_project(_frag("episodic", {"event_time": "2026-06-05T00:00:00Z"})) is False


def test_procedural_does_not_project_goes_to_skills():
    assert should_project(_frag("procedural", {"steps": ["a", "b"]})) is False


def test_resource_deferred_not_projected():
    assert should_project(_frag("resource", {"ref": "file://x"})) is False


# --- transform carries provenance + scope ---------------------------------- #
def test_fragment_to_chunk_carries_ref_type_scope():
    f = _frag("semantic", {"summary": "use worktrees for parallel sessions"})
    chunk = fragment_to_chunk(f)
    assert isinstance(chunk, ProjectedChunk)
    assert chunk.fragment_ref == f.id          # provenance survives retrieval
    assert chunk.cognitive_type == "semantic"
    assert chunk.chunk_text == "use worktrees for parallel sessions"
    assert chunk.principal_id == "@ben"        # (owner) scope
    assert chunk.agents == ("@axi",)           # (agent) scope
    assert chunk.source_type == "memory"


def test_text_render_falls_back_to_json():
    f = _frag("semantic", {"k": "v", "n": 3})
    chunk = fragment_to_chunk(f)
    assert '"k": "v"' in chunk.chunk_text


# --- never-project floor holds against a direct call ----------------------- #
def test_fragment_to_chunk_refuses_vault():
    with pytest.raises(ValueError, match="only semantic"):
        fragment_to_chunk(_frag("vault", {"secret": "x"}))


# --- batch filters to projectable only ------------------------------------- #
def test_project_fragments_filters_mixed_batch():
    frags = [
        _frag("semantic", {"summary": "a"}),
        _frag("vault", {"secret": "s"}),
        _frag("episodic", {"event_time": "2026-06-05T00:00:00Z"}),
        _frag("semantic", {"summary": "b"}),
        _frag("procedural", {"steps": ["x"]}),
    ]
    chunks = project_fragments(frags)
    assert len(chunks) == 2
    assert {c.chunk_text for c in chunks} == {"a", "b"}
    assert all(c.cognitive_type == "semantic" for c in chunks)
