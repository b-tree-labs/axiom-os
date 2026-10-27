# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for T0-3 prompt-composition observability writer."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from axiom.infra.prompt_composer import PromptComposer
from axiom.infra.prompt_observability import log_prompt_composition


class TestJsonlFallback:
    def test_writes_jsonl_when_no_composition(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        composer = PromptComposer()
        composer.add("identity", name="persona", content="X", source="axiom")
        log_prompt_composition(
            composer.observability_payload(),
            session_id="sess-1",
            principal_id="@alice:ut",
        )
        jsonl = tmp_path / "sessions" / "sess-1" / "prompt_compositions.jsonl"
        assert jsonl.exists()
        rec = json.loads(jsonl.read_text().splitlines()[0])
        assert rec["session_id"] == "sess-1"
        assert rec["fact_kind"] == "prompt_composition"
        assert rec["layer_counts"]["identity"] >= 1

    def test_appends_across_turns(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        c = PromptComposer()
        c.add("identity", name="p", content="x", source="axiom")
        log_prompt_composition(c.observability_payload(), session_id="s")
        log_prompt_composition(c.observability_payload(), session_id="s")
        jsonl = tmp_path / "sessions" / "s" / "prompt_compositions.jsonl"
        assert len(jsonl.read_text().strip().splitlines()) == 2


class TestCompositionPath:
    def test_prefers_composition_write(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        composition = MagicMock()
        c = PromptComposer()
        c.add("identity", name="p", content="x", source="axiom")
        log_prompt_composition(
            c.observability_payload(),
            session_id="s", principal_id="@a:b",
            composition=composition,
        )
        composition.write.assert_called_once()
        kwargs = composition.write.call_args.kwargs
        assert kwargs["cognitive_type"] == "episodic"
        assert kwargs["content"]["fact_kind"] == "prompt_composition"
        # JSONL not written when composition succeeded
        jsonl = tmp_path / "sessions" / "s" / "prompt_compositions.jsonl"
        assert not jsonl.exists()

    def test_falls_back_to_jsonl_on_composition_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        composition = MagicMock()
        composition.write.side_effect = RuntimeError("boom")
        c = PromptComposer()
        c.add("identity", name="p", content="x", source="axiom")
        log_prompt_composition(
            c.observability_payload(),
            session_id="s",
            composition=composition,
        )
        jsonl = tmp_path / "sessions" / "s" / "prompt_compositions.jsonl"
        assert jsonl.exists()


class TestResilience:
    def test_never_raises_on_disk_error(self, tmp_path, monkeypatch):
        # Point at a file path instead of a dir — writes will fail.
        blocker = tmp_path / "blocked"
        blocker.write_text("i am a file")
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(blocker))
        c = PromptComposer()
        c.add("identity", name="p", content="x", source="axiom")
        # Must not raise even though sessions/ can't be created under a file.
        log_prompt_composition(c.observability_payload(), session_id="s")
