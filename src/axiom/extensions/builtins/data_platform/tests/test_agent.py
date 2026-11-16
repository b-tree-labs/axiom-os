# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the PLINTH data-platform orchestrator agent skeleton.

PLINTH orchestrates ingestion / source-monitoring / pack-flow. At
skeleton stage its job-dispatch interface is exercised against a fake
``IngestSource`` — there is no real scheduler and no real ingest engine
call.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


class FakeIngestSource:
    name = "fake-source"

    def __init__(self) -> None:
        self.listed_since: list[datetime | None] = []
        self.fetched: list[str] = []

    def list_changed(self, since: datetime | None = None) -> list[str]:
        self.listed_since.append(since)
        return ["a", "b", "c"]

    def fetch(self, item: str):
        from axiom.extensions.builtins.data_platform.contracts import FetchedItem

        self.fetched.append(item)
        content = f"payload::{item}".encode()
        return FetchedItem(
            source_name=self.name,
            item_id=item,
            display_name=f"{item}.bin",
            content=content,
            content_type=None,
            size=len(content),
            modified_at=None,
            etag=None,
            source_path=None,
            extra={},
        )


def _agent():
    from axiom.extensions.builtins.data_platform import PlinthAgent

    return PlinthAgent()


def test_agent_name_is_all_caps():
    from axiom.extensions.builtins.data_platform import PlinthAgent

    assert PlinthAgent.name == "PLINTH"


def test_run_scheduled_ingest_polls_and_fetches():
    agent = _agent()
    src = FakeIngestSource()
    report = agent.run_scheduled_ingest(src)
    # The skeleton dispatches the poll then a fetch per changed item.
    assert src.listed_since == [None]
    assert src.fetched == ["a", "b", "c"]
    assert report.source == "fake-source"
    assert report.items_seen == 3
    assert report.items_fetched == 3


def test_run_scheduled_ingest_passes_since_through():
    agent = _agent()
    src = FakeIngestSource()
    since = datetime(2026, 5, 1)
    agent.run_scheduled_ingest(src, since=since)
    assert src.listed_since == [since]


def test_run_scheduled_ingest_dry_run_skips_fetch():
    agent = _agent()
    src = FakeIngestSource()
    report = agent.run_scheduled_ingest(src, dry_run=True)
    assert src.fetched == []
    assert report.items_seen == 3
    assert report.items_fetched == 0
    assert report.dry_run is True


def test_agent_registry_is_a_data_platform_registry():
    from axiom.extensions.builtins.data_platform import DataPlatformRegistry

    agent = _agent()
    assert isinstance(agent.registry, DataPlatformRegistry)


def test_agent_can_dispatch_a_registered_source_by_name():
    agent = _agent()
    src = FakeIngestSource()
    agent.registry.register_source(src)
    report = agent.run_scheduled_ingest("fake-source")
    assert report.items_fetched == 3


def test_persona_file_exists():
    persona = (
        Path(__file__).resolve().parent.parent / "agents" / "plinth" / "persona.md"
    )
    assert persona.exists()
    assert persona.read_text(encoding="utf-8").strip()
