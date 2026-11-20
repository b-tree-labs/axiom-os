# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The reads (list, status, diagnose, events) reach MCP; nothing that changes a node's capture does.

Built the way the unified MCP server builds its surface (discovery, each
extension's ``bind``, the shared projector), and called through the same
dispatch handler a harness reaches, so a tool that is listed is also one that
answers.
"""

from __future__ import annotations

import asyncio

import pytest

from axiom.extensions.builtins.video import connector as vc

READS = {
    "axiom_video__list",
    "axiom_video__status",
    "axiom_video__diagnose",
    "axiom_video__events",
}
WRITES = {
    "axiom_video__add",
    "axiom_video__verify",
    "axiom_video__start",
    "axiom_video__stop",
    "axiom_video__install",
    "axiom_video__uninstall",
    "axiom_video__revoke",
}


@pytest.fixture
def surface(state_dir):
    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry

    return AggregationRegistry.from_node().build()


def test_the_reads_are_listed_and_the_writes_are_not(surface):
    names = {t.name for t in surface.tools if t.name.startswith("axiom_video__")}
    assert names == READS
    assert not names & WRITES


def test_the_reads_say_they_change_nothing(surface):
    for tool in (t for t in surface.tools if t.name in READS):
        assert tool.annotations.read_only_hint is True, tool.name


def test_status_answers_over_mcp_without_a_credential(state_dir, surface, login):
    vc.connect(
        vc.VideoConnector(
            name="cam-1", credential_ref=login.ref, source={"kind": "rtsp", "url": "rtsp://h/ok"}
        ),
        state_dir=state_dir,
    )
    answer = asyncio.run(surface.dispatch["axiom_video__status"]({}))
    assert answer["ok"], answer
    assert [s["name"] for s in answer["value"]["sources"]] == ["cam-1"]
    assert login.password not in repr(answer)


def test_an_undeclared_argument_is_refused_not_forwarded(surface):
    answer = asyncio.run(surface.dispatch["axiom_video__status"]({"url": "rtsp://elsewhere/"}))
    assert answer["ok"] is False and "url" in answer["errors"][0]
