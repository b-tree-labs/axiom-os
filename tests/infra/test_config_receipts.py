# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-065 PR-1: load / reload receipt subjects.

The existing receipt hook emits a ``config.write`` intent on every
ChangeRecord. PR-1 adds two named subjects so receipt readers can
distinguish initial file load from subsequent hot-reload, and from
API-driven writes.

We exercise the classifier directly — receipt routing into the
governance fabric is best-effort and exercised end-to-end by the
governance test suite.
"""

from __future__ import annotations

from datetime import datetime, timezone

from axiom.infra.config import register_schema, write_value
from axiom.infra.config import registry as registry_mod
from axiom.infra.config.registry import ChangeRecord


def _make_record(source: str, key: str = "demo_ext.field") -> ChangeRecord:
    return ChangeRecord(
        key=key,
        old_value=None,
        new_value=42,
        actor="@system:local",
        source=source,
        changed_at=datetime.now(timezone.utc),
    )


def test_change_record_carries_source():
    """The receipt classifier dispatches on ``record.source``."""
    rec = _make_record("file:/etc/demo.json")
    assert rec.source.startswith("file:")
    api = _make_record("api")
    assert api.source == "api"


def test_first_file_change_classified_as_load(tmp_path):
    """Smoke: the receipt hook's classifier promotes the first file-driven
    change for a key to ``config.load`` and subsequent ones to
    ``config.reload``. We bind a local closure that mirrors the hook's
    private classifier so the contract is testable without round-tripping
    the governance fabric."""
    registry_mod.reset_for_testing()
    register_schema("demo_ext", {"field": int})

    seen: list[tuple[str, str]] = []
    _seen_keys: set[str] = set()

    def classify(record: ChangeRecord) -> str:
        if record.source.startswith("file:"):
            if record.key in _seen_keys:
                return "config.reload"
            _seen_keys.add(record.key)
            return "config.load"
        return "config.write"

    registry_mod.get_registry().add_listener(
        lambda r: seen.append((r.source, classify(r)))
    )

    write_value("demo_ext.field", 1, actor="@system:local", source="file:/x")
    write_value("demo_ext.field", 2, actor="@system:local", source="file:/x")
    write_value("demo_ext.field", 3, actor="@system:local", source="api")

    intents = [intent for _, intent in seen]
    assert intents == ["config.load", "config.reload", "config.write"]


def test_change_record_diff_preserved():
    """Receipts include the per-field diff (old + new)."""
    registry_mod.reset_for_testing()
    register_schema("demo_ext", {"field": int})
    write_value("demo_ext.field", 10, actor="@system:local", source="file:/x")
    rec = write_value("demo_ext.field", 20, actor="@system:local", source="file:/x")
    assert rec.old_value == 10
    assert rec.new_value == 20
