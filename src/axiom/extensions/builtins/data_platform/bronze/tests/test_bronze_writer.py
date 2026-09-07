# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``BronzeWriter`` — the provenance-gated bronze ingest seam.

The writer routes every FetchedItem through the v0.22.0 provenance gate
(``axiom.rag.ingest_router.route_path``) before any external mutation,
then dispatches to the configured sink. EXCLUDE writes only a decision
record (never content). ALLOW + QUARANTINE go to the sink with their
disposition stamped on the BronzeWriteResult.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from axiom.rag.ingest_router import Disposition, ProvenanceRule


def _item(
    *,
    source_name: str = "box-reports",
    item_id: str = "1",
    display_name: str = "doc.pdf",
    content: bytes = b"%PDF-1.7 sample",
    source_path: str | None = "/Reports/doc.pdf",
    modified_at: datetime | None = None,
    etag: str | None = "v1",
    extra: dict[str, str] | None = None,
):
    from axiom.extensions.builtins.data_platform.contracts import FetchedItem

    return FetchedItem(
        source_name=source_name,
        item_id=item_id,
        display_name=display_name,
        content=content,
        content_type="application/pdf",
        size=len(content),
        modified_at=modified_at or datetime(2026, 5, 29, 12, 0, tzinfo=UTC),
        etag=etag,
        source_path=source_path,
        extra=extra or {},
    )


# ---------- gate routing ----------------------------------------------------


def test_writer_routes_through_provenance_gate_and_records_decision(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )

    rules = [
        ProvenanceRule(
            pattern="/Reports/",
            disposition=Disposition.ALLOW,
            tier="community",
            reason="reports folder",
        )
    ]
    writer = BronzeWriter(
        rules=rules,
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    )
    result = writer.write(_item())

    assert result.disposition is Disposition.ALLOW
    assert result.tier == "community"
    assert result.matched_rule == "/Reports/"
    assert result.content_path is not None
    assert result.content_path.exists()


def test_writer_quarantines_when_no_rule_matches_and_default_is_quarantine(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )

    writer = BronzeWriter(
        rules=[],
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    )
    result = writer.write(_item(source_path="/Unknown/x.pdf"))

    assert result.disposition is Disposition.QUARANTINE
    assert result.matched_rule is None
    # Quarantine still writes content + sidecar — humans review there.
    assert result.content_path is not None and result.content_path.exists()


def test_writer_excludes_item_without_writing_content(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )

    rules = [
        ProvenanceRule(
            pattern="/Licensed/",
            disposition=Disposition.EXCLUDE,
            reason="licensed-vendor source",
        )
    ]
    writer = BronzeWriter(
        rules=rules,
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.ALLOW,
        default_tier="community",
    )
    result = writer.write(_item(source_path="/Licensed/proprietary.zip"))

    assert result.disposition is Disposition.EXCLUDE
    assert result.content_hash is None  # EXCLUDE writes no content
    assert result.content_path is None
    assert result.record_path.exists()  # but the decision IS recorded


# ---------- content-addressed blob -----------------------------------------


def test_writer_content_addresses_blob_by_sha256(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )

    content = b"deterministic content"
    sha = hashlib.sha256(content).hexdigest()
    writer = BronzeWriter(
        rules=[ProvenanceRule(pattern="/x/", disposition=Disposition.ALLOW, tier="community")],
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    )
    result = writer.write(_item(content=content, source_path="/x/y.pdf"))
    assert result.content_hash == sha
    assert sha in str(result.content_path)


def test_writer_idempotent_on_same_content_hash(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )

    writer = BronzeWriter(
        rules=[ProvenanceRule(pattern="/x/", disposition=Disposition.ALLOW, tier="community")],
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    )
    first = writer.write(_item(content=b"same bytes", source_path="/x/y.pdf"))
    second = writer.write(_item(content=b"same bytes", source_path="/x/y.pdf"))

    # Same blob path; second write does not duplicate the content blob.
    assert first.content_path == second.content_path
    assert first.content_hash == second.content_hash


# ---------- sidecar manifest ------------------------------------------------


def test_sidecar_carries_fetched_item_metadata_and_decision(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )

    writer = BronzeWriter(
        rules=[ProvenanceRule(pattern="/Reports/", disposition=Disposition.ALLOW, tier="community")],
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    )
    result = writer.write(_item(extra={"sha1": "deadbeef"}))

    sidecar = json.loads(result.record_path.read_text())
    assert sidecar["source_name"] == "box-reports"
    assert sidecar["item_id"] == "1"
    assert sidecar["display_name"] == "doc.pdf"
    assert sidecar["source_path"] == "/Reports/doc.pdf"
    assert sidecar["disposition"] == "allow"
    assert sidecar["tier"] == "community"
    assert sidecar["matched_rule"] == "/Reports/"
    assert sidecar["content_sha256"] == result.content_hash
    assert sidecar["etag"] == "v1"
    assert sidecar["extra"]["sha1"] == "deadbeef"
    assert "fetched_at" in sidecar  # writer-stamped


def test_writer_uses_item_id_when_source_path_missing(tmp_path: Path):
    """No source_path → route uses ``<source_name>/<item_id>`` as the rel-path."""
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )

    rules = [ProvenanceRule(pattern="box-reports/*", disposition=Disposition.ALLOW, tier="community")]
    writer = BronzeWriter(
        rules=rules,
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    )
    result = writer.write(_item(source_path=None))
    assert result.disposition is Disposition.ALLOW
    assert result.matched_rule == "box-reports/*"


# ---------- tier resolution -------------------------------------------------


def test_default_tier_applied_when_rule_omits_tier(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )

    # Rule allows but doesn't specify a tier — writer falls back to default_tier.
    rules = [ProvenanceRule(pattern="/Reports/", disposition=Disposition.ALLOW)]
    writer = BronzeWriter(
        rules=rules,
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.QUARANTINE,
        default_tier="community",
    )
    result = writer.write(_item())
    assert result.tier == "community"
