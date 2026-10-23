# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for neut signal data models."""

from axiom.extensions.builtins.signals.models import (
    Changelog,
    ChangelogEntry,
    Extraction,
    Signal,
)


class TestSignal:
    """Unit tests for the Signal dataclass."""

    def test_default_construction(self):
        s = Signal(source="test", timestamp="2026-01-01T00:00:00Z", raw_text="hello")
        assert s.source == "test"
        assert s.signal_type == "raw"
        assert s.confidence == 0.5
        assert s.people == []
        assert s.initiatives == []
        assert s.metadata == {}

    def test_full_construction(self):
        s = Signal(
            source="gitlab_diff",
            timestamp="2026-02-15T10:00:00Z",
            raw_text="commit message",
            people=["Alice"],
            initiatives=["Project Alpha"],
            signal_type="progress",
            detail="Alice committed code",
            confidence=1.0,
            metadata={"sha": "abc123"},
        )
        assert s.people == ["Alice"]
        assert s.confidence == 1.0
        assert s.metadata["sha"] == "abc123"

    def test_to_dict_roundtrip(self):
        original = Signal(
            source="freetext",
            timestamp="2026-02-15T10:00:00Z",
            raw_text="some text",
            people=["Bob"],
            initiatives=["Beta"],
            signal_type="action_item",
            detail="Bob needs to review",
            confidence=0.8,
            metadata={"filename": "note.md"},
        )
        d = original.to_dict()
        restored = Signal.from_dict(d)

        assert restored.source == original.source
        assert restored.people == original.people
        assert restored.confidence == original.confidence
        assert restored.metadata == original.metadata

    def test_from_dict_ignores_unknown_keys(self):
        data = {
            "source": "test",
            "timestamp": "2026-01-01T00:00:00Z",
            "raw_text": "hello",
            "unknown_key": "should be ignored",
        }
        s = Signal.from_dict(data)
        assert s.source == "test"
        assert not hasattr(s, "unknown_key")


class TestExtraction:
    """Unit tests for the Extraction dataclass."""

    def test_empty_extraction(self):
        e = Extraction(extractor="test", source_file="input.txt")
        assert e.signals == []
        assert e.errors == []
        assert e.extracted_at  # Should have auto-generated timestamp

    def test_extraction_with_signals(self):
        signals = [
            Signal(source="test", timestamp="2026-01-01T00:00:00Z", raw_text="a"),
            Signal(source="test", timestamp="2026-01-01T00:00:00Z", raw_text="b"),
        ]
        e = Extraction(extractor="test", source_file="input.txt", signals=signals)
        assert len(e.signals) == 2

    def test_to_dict(self):
        e = Extraction(
            extractor="gitlab_diff",
            source_file="export.json",
            signals=[Signal(source="test", timestamp="now", raw_text="x")],
            errors=["warning"],
        )
        d = e.to_dict()
        assert d["extractor"] == "gitlab_diff"
        assert len(d["signals"]) == 1
        assert d["errors"] == ["warning"]


class TestChangelog:
    """Unit tests for Changelog and ChangelogEntry."""

    def test_changelog_entry(self):
        entry = ChangelogEntry(
            initiative="Alpha",
            signal_type="progress",
            detail="Feature shipped",
            people=["Alice"],
            sources=["gitlab_diff"],
            confidence=1.0,
        )
        assert entry.initiative == "Alpha"

    def test_changelog_with_entries(self):
        cl = Changelog(
            date="2026-02-15",
            entries=[
                ChangelogEntry(initiative="A", signal_type="progress", detail="Done"),
            ],
            summary="Good week",
        )
        assert cl.date == "2026-02-15"
        assert len(cl.entries) == 1
        assert cl.generated_at  # Auto-generated
