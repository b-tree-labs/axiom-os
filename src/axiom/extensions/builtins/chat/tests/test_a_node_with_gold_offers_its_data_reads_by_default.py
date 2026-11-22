# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A node that holds its own gold offers its data reads in chat, with nothing to configure.

Seen live on a development node with sample gold: a chart question got no chart, because chat
offered no data tool until somebody set ``chat.tool_namespaces = ["data"]`` by hand. A bound
laptop already gets the node's served reads whatever that setting says (``bound_tools``); a node
that holds the medallion itself got nothing. It now gets the same served reads, read-only, through
the same projection, and only where the existing checks say there is a medallion here and its
database answers (``holds_medallion``, ``chat.local_data_tools``).
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat import tools as chat_tools
from axiom.extensions.builtins.data_platform import read_target
from axiom.infra import serving_mode
from axiom.infra.orchestrator.actions import ActionCategory


class _Settings:
    def get(self, key, default=None):
        return default


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(serving_mode.SAMPLE_DATA_ENV, raising=False)
    monkeypatch.setattr("axiom.extensions.builtins.settings.store.SettingsStore", _Settings)
    monkeypatch.setattr(read_target, "holds_medallion", lambda: True)
    monkeypatch.setattr(chat_tools, "_node_database_reachable", lambda: True)


def test_an_unbound_node_with_gold_offers_the_served_reads_and_only_reads(node):
    offered = chat_tools.get_all_tools()
    assert {"data__series", "data__latest", "data__aggregate"} <= set(offered)
    data = {n: t for n, t in offered.items() if n.startswith("data__")}
    assert all(t.category == ActionCategory.READ for t in data.values()), sorted(data)
    assert chat_tools.execute_tool("data__nonesuch", {})["error"].startswith("Unknown tool")


def test_a_data_question_on_that_node_is_a_data_turn_like_a_bound_one(node):
    """The rule that a value is read only by calling a data tool, and the data tools preferred when
    the model's window cannot hold every tool, applied only to a bound install. A small model on a
    node holding its own gold then answered a chart question from memory, in prose."""
    from axiom.extensions.builtins.chat import bound_tools

    offered = chat_tools.get_all_tools()
    turn = bound_tools.data_turn("Chart temperature.inlet on 2026-11-12", offered)
    assert turn is not None and turn.is_data
    assert {"data__series", "data__latest"} <= turn.data_tools
    assert "data__series" in turn.rule() and "bound" not in turn.rule()
