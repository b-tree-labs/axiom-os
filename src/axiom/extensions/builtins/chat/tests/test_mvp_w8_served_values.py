# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""W8: a chat answer on gold carries each value's unit, source, as-of and quality,
names a held span in words, and cites the gold object or Library item it used.

MVP-02-A1, MVP-02-A2, MVP-18-A1 (the chat half).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from axiom.extensions.builtins.chat.scope import ChatScope
from axiom.extensions.builtins.chat.served_values import served_values
from axiom.extensions.builtins.data_platform.company import QUALITY
from axiom.extensions.builtins.data_platform.marks import MARKS

_KEYS = {"label", "value", "unit", "source", "as_of", "quality", "mark", "mark_reason", "ref"}


def _latest(readings: list[dict[str, Any]]) -> dict[str, Any]:
    """A data.latest result as the tool loop sees it."""
    return {
        "ok": True,
        "value": {
            "data": {"site": "site-a", "readings": readings, "unfetched": 0},
            "provenance": {
                "source": "gold.signal_catalogue, gold.signals",
                "method": "latest",
                "site": "site-a",
            },
        },
    }


def _show(points: list[dict[str, Any]], unit: str = "kW") -> dict[str, Any]:
    """A data.show result: flat channel-tagged points from gold.signals."""
    return {
        "ok": True,
        "value": {
            "data": {"site": "site-a", "target": "Power", "series": points, "units": [unit]},
            "provenance": {
                "source": "gold.signals",
                "method": "mean(value) by 1 minute",
                "site": "site-a",
            },
            "unit": {"value": unit},
        },
    }


# --- MVP-02-A1: unit, source, as-of, quality ---------------------------------------------


def test_mvp_02_a1_a_latest_reading_carries_unit_source_as_of_and_quality() -> None:
    [card] = served_values(
        [
            (
                "data.latest",
                {"site": "site-a"},
                _latest(
                    [
                        {
                            "feed": "f",
                            "channel": "Power",
                            "unit": "kW",
                            "ts": "2026-10-30T14:00:00Z",
                            "value": 902.0,
                            "quality": "good",
                        },
                    ]
                ),
            )
        ]
    )
    assert set(card) == _KEYS
    assert card["label"] == "Power" and card["value"] == 902.0 and card["unit"] == "kW"
    assert card["source"] == "gold.signals"
    assert card["as_of"] == "2026-10-30T14:00:00Z"
    assert card["quality"] == "good" and card["mark"] is None and card["mark_reason"] is None
    assert card["ref"] == {"kind": "gold", "id": "gold.signals", "title": "Power"}


def test_mvp_02_a1_missing_quality_is_null_never_good() -> None:
    [card] = served_values(
        [
            (
                "data.latest",
                {},
                _latest(
                    [
                        {
                            "feed": "f",
                            "channel": "T1",
                            "unit": "degC",
                            "ts": "2026-10-30T14:00:00Z",
                            "value": 21.5,
                        },
                    ]
                ),
            )
        ]
    )
    assert card["quality"] is None


def test_mvp_02_a1_a_value_with_no_timestamp_has_null_as_of() -> None:
    [card] = served_values(
        [
            (
                "data.aggregate",
                {"column": "value"},
                {
                    "ok": True,
                    "value": {
                        "data": {"table": "signals", "column": "value", "fn": "max", "value": 3.0},
                        "provenance": {"source": "gold.signals", "method": "max(value)"},
                        "unit": {"value": "W"},
                    },
                },
            )
        ]
    )
    assert card["value"] == 3.0 and card["unit"] == "W" and card["as_of"] is None
    assert card["quality"] is None and card["source"] == "gold.signals"


def test_mvp_02_a1_quality_and_mark_stay_in_their_closed_vocabularies() -> None:
    [card] = served_values(
        [
            (
                "data.latest",
                {},
                _latest(
                    [
                        {
                            "channel": "X",
                            "unit": "",
                            "ts": "2026-10-30T14:00:00Z",
                            "value": 1.0,
                            "quality": "excellent",
                        },
                    ]
                ),
            )
        ]
    )
    assert card["quality"] is None  # not one of QUALITY: not passed through as if it were
    for c in served_values(
        [
            (
                "data.show",
                {},
                _show(
                    [
                        {
                            "t": "2026-10-30T14:00:00Z",
                            "value": 1.0,
                            "channel": "P",
                            "quality": "stale",
                        },
                    ]
                ),
            )
        ]
    ):
        assert c["quality"] in (*QUALITY, None) and c["mark"] in (*MARKS, None)


def test_mvp_02_a1_a_series_serves_its_newest_present_value() -> None:
    [card] = served_values(
        [
            (
                "data.show",
                {"target": "Power"},
                _show(
                    [
                        {"t": "2026-10-30T14:00:00Z", "value": 1.0, "channel": "Power"},
                        {"t": "2026-10-30T14:01:00Z", "value": 2.0, "channel": "Power"},
                    ]
                ),
            )
        ]
    )
    assert card["value"] == 2.0 and card["as_of"] == "2026-10-30T14:01:00Z" and card["unit"] == "kW"


def test_mvp_02_a1_a_read_that_is_not_gold_serves_no_value() -> None:
    silver = {
        "ok": True,
        "value": {"data": {"value": 1.0}, "provenance": {"source": "silver.signals"}},
    }
    assert served_values([("data.aggregate", {}, silver)]) == []
    assert served_values([("data.aggregate", {}, {"error": "refused"})]) == []


def test_mvp_02_a1_the_browser_receives_a_served_values_frame() -> None:
    from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider

    frames: list[dict] = []
    values = served_values(
        [
            (
                "data.latest",
                {},
                _latest(
                    [
                        {
                            "channel": "Power",
                            "unit": "kW",
                            "ts": "2026-10-30T14:00:00Z",
                            "value": 902.0,
                        },
                    ]
                ),
            )
        ]
    )
    SseRenderProvider(frames.append).render_served_values(values)
    assert frames == [{"served_values": values}]
    assert json.loads(json.dumps(frames[0]))["served_values"][0]["source"] == "gold.signals"
    SseRenderProvider(frames.append).render_served_values([])
    assert len(frames) == 1, "an answer that served nothing sends no frame"


def _agent(render: Any) -> Any:
    from axiom.extensions.builtins.chat.agent import ChatAgent

    agent = ChatAgent.__new__(ChatAgent)
    agent._render = render  # type: ignore[attr-defined]
    return agent


def test_mvp_02_a1_the_agent_emits_the_turns_values_once_and_forgets_them() -> None:
    from axiom.extensions.builtins.chat.agent import _collect_served_read

    sent: list[list[dict]] = []
    agent = _agent(SimpleNamespace(render_served_values=sent.append))
    scope = ChatScope()
    _collect_served_read(
        scope,
        "data__latest",
        {"site": "site-a"},
        _latest(
            [
                {"channel": "Power", "unit": "kW", "ts": "2026-10-30T14:00:00Z", "value": 902.0},
            ]
        ),
    )
    _collect_served_read(scope, "read_file", {"path": "/x"}, {"content": "hello"})
    agent._emit_served_values(scope=scope)
    assert len(sent) == 1 and [c["label"] for c in sent[0]] == ["Power"]
    assert scope.served_reads == []


def test_mvp_02_a1_a_renderer_without_value_cards_is_skipped_and_nothing_raises() -> None:
    from axiom.extensions.builtins.chat.agent import _collect_served_read

    agent = _agent(SimpleNamespace())  # a terminal: no render_served_values
    scope = ChatScope()
    _collect_served_read(
        scope, "data__latest", {}, _latest([{"channel": "P", "value": 1.0, "ts": "x"}])
    )
    agent._emit_served_values(scope=scope)

    def boom(_values: Any) -> None:
        raise RuntimeError("renderer broke")

    agent = _agent(SimpleNamespace(render_served_values=boom))
    _collect_served_read(
        scope, "data__latest", {}, _latest([{"channel": "P", "value": 1.0, "ts": "x"}])
    )
    agent._emit_served_values(scope=scope)  # never raises


# --- MVP-18-A1: a held span is named in the answer ----------------------------------------


_HELD = [
    {"t": "2026-10-30T14:00:00Z", "value": 900.0, "channel": "Power", "quality": "good"},
    {
        "t": "2026-10-30T14:02:00Z",
        "value": None,
        "channel": "Power",
        "quality": "bad",
        "quality_reason": "company.a_vs_b",
    },
    {"t": "2026-10-30T14:20:00Z", "value": None, "channel": "Power", "quality": "bad"},
    {"t": "2026-10-30T14:22:00Z", "value": 905.0, "channel": "Power", "quality": "good"},
]


def test_mvp_18_a1_a_held_span_marks_the_served_value() -> None:
    [card] = served_values([("data.show", {"target": "Power"}, _show(_HELD))])
    assert card["value"] == 905.0
    assert card["mark"] == "held" and card["mark_reason"] == "company.a_vs_b"


def test_mvp_18_a1_the_answer_names_the_held_span_in_words() -> None:
    from axiom.extensions.builtins.chat.agent import _collect_served_read

    agent = _agent(None)
    scope = ChatScope()
    _collect_served_read(scope, "data__show", {"target": "Power"}, _show(_HELD))
    line = agent._marked_spans_line(scope=scope)
    assert "Power held from 14:02 to 14:20 (company.a_vs_b)" in line


def test_mvp_18_a1_an_unmarked_turn_adds_nothing_to_the_answer() -> None:
    from axiom.extensions.builtins.chat.agent import _collect_served_read

    scope = ChatScope()
    _collect_served_read(scope, "data__show", {}, _show(_HELD[:1]))
    assert _agent(None)._marked_spans_line(scope=scope) == ""


def test_mvp_18_a1_the_turn_footer_carries_the_marked_line() -> None:
    """The footer is how a turn's own reads reach the answer text; the marked line rides it."""
    import inspect

    from axiom.extensions.builtins.chat import agent as agent_mod

    src = inspect.getsource(agent_mod.ChatAgent._turn_impl)
    assert "_marked_spans_line" in src and "answer_footer" in src


# --- MVP-02-A2: a citation targets a gold object or a Library item -------------------------


def test_mvp_02_a2_a_data_read_cites_its_gold_object() -> None:
    from axiom.extensions.builtins.chat.answer_sources import data_read_sources
    from axiom.extensions.builtins.chat.providers.base import citation_entries

    srcs = data_read_sources(
        [
            {"tool": "data.show", "args": {"site": "site-a"}, "source": "gold.signals"},
            {"tool": "data.aggregate", "args": {"table": "runs"}},
        ],
        [],
    )
    entries = citation_entries(srcs)
    assert [e["ref"] for e in entries] == [
        {"kind": "gold", "id": "gold.signals"},
        {"kind": "gold", "id": "gold.runs"},
    ]
    for e in entries:  # the existing keys are unchanged
        assert {"key", "rank", "title", "path", "corpus", "url"} <= set(e)


def test_mvp_02_a2_a_document_cites_its_library_item() -> None:
    from axiom.extensions.builtins.chat.providers.base import citation_entries
    from axiom.rag.retriever import RetrievedChunk

    chunk = RetrievedChunk(
        citation_key="C1",
        rank=1,
        source_path="/lib/ops/rod.md",
        source_title="Rod",
        chunk_text="b",
        chunk_index=0,
        corpus="rag-org",
        similarity=0.8,
        rrf_score=0.5,
    )
    [entry] = citation_entries([chunk])
    assert entry["ref"] == {"kind": "library", "id": "/lib/ops/rod.md"}
    assert entry["key"] == "C1" and entry["path"] == "/lib/ops/rod.md"


def test_mvp_02_a2_a_read_with_no_gold_object_and_a_silver_table_open_nothing() -> None:
    from axiom.extensions.builtins.chat.answer_sources import citation_ref, data_read_sources

    srcs = data_read_sources([{"tool": "data.x", "args": {"table": "silver.signals"}}], ["data.y"])
    assert len(srcs) == 2 and [citation_ref(s) for s in srcs] == [None, None]


def test_mvp_02_a2_the_tool_loop_records_the_gold_object_on_the_read() -> None:
    from axiom.extensions.builtins.chat.agent import _collect_served_read, _note_data_read

    scope = ChatScope()
    scope.session.context["turn_data"] = {"data_tools": ["data__show"]}
    _note_data_read(scope, "data__show", {"target": "Power"})
    _collect_served_read(scope, "data__show", {"target": "Power"}, _show(_HELD))
    [read] = scope.session.context["turn_data"]["data_reads"]
    assert read["source"] == "gold.signals"
