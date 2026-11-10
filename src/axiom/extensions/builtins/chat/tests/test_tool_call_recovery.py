# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A tool call the model wrote as prose must not reach the user as an answer.

Observed against a real node: asked how to pull a week of telemetry, chat
replied with fenced ```tool blocks naming `telemetry_aggregate`, invented a
metric (`Shim1_position`, which does not exist) and an argument (`window: 7d`,
which is not in the signature), and offered to "try one of these". Nothing ran.
The tools were registered and had been offered natively — the provider's
OpenAI-compatible endpoint accepts a `tools` parameter and ignores it, so the
model improvised the protocol from the system prompt.

The user cannot tell that apart from an answer. They read a confident paragraph
containing a metric name that does not exist and go and use it.

Recovery is validated against the live tool table, so it works for every
extension's tools without any per-extension wiring — a site that ships its own
extension gets this the moment its tools are registered.
"""

from __future__ import annotations

from axiom.extensions.builtins.chat.tool_call_recovery import (
    looks_like_a_tool_attempt,
    recover_tool_calls,
)

REGISTERED = {"telemetry_aggregate": object(), "read_file": object()}


def test_recovers_a_fenced_tool_block():
    """The shape the node actually produced."""
    text = """Here's how:

```tool
{"name": "telemetry_aggregate", "arguments": {"metric": "Shim1", "start": "2026-05-01"}}
```
"""
    calls = recover_tool_calls(text, REGISTERED)

    assert [c.name for c in calls] == ["telemetry_aggregate"]
    assert calls[0].input == {"metric": "Shim1", "start": "2026-05-01"}


def test_recovers_the_legacy_bracket_form():
    """The one format the old parser knew, kept working."""
    text = '[tool: read_file] {"path": "a.txt"}'

    calls = recover_tool_calls(text, REGISTERED)

    assert [c.name for c in calls] == ["read_file"]
    assert calls[0].input == {"path": "a.txt"}


def test_recovers_a_plain_json_fence():
    """Models emit ```json as readily as ```tool."""
    text = '```json\n{"name": "read_file", "parameters": {"path": "b.txt"}}\n```'

    calls = recover_tool_calls(text, REGISTERED)

    assert [c.name for c in calls] == ["read_file"]
    assert calls[0].input == {"path": "b.txt"}


def test_recovers_several_in_one_answer():
    text = (
        '```tool\n{"name": "read_file", "arguments": {"path": "a"}}\n```\n'
        'and then\n'
        '```tool\n{"name": "telemetry_aggregate", "arguments": {"metric": "Shim1"}}\n```'
    )

    assert [c.name for c in recover_tool_calls(text, REGISTERED)] == [
        "read_file",
        "telemetry_aggregate",
    ]


def test_an_unregistered_name_is_never_executed():
    """The safety property. Recovery runs on model prose, so the tool table —
    not the text — decides what may run."""
    text = '```tool\n{"name": "rm_rf", "arguments": {"path": "/"}}\n```'

    assert recover_tool_calls(text, REGISTERED) == []


def test_ordinary_json_in_an_answer_is_not_a_tool_call():
    """Negative control: showing someone a JSON payload must stay prose."""
    text = (
        "The endpoint returns:\n\n"
        '```json\n{"metric": "Shim1", "count": 5000, "truncated": true}\n```'
    )

    assert recover_tool_calls(text, REGISTERED) == []
    assert looks_like_a_tool_attempt(text, REGISTERED) is False


def test_prose_about_a_tool_is_not_a_tool_call():
    """Explaining `telemetry_aggregate` must not invoke it."""
    text = "Use telemetry_aggregate when you want statistics rather than points."

    assert recover_tool_calls(text, REGISTERED) == []
    assert looks_like_a_tool_attempt(text, REGISTERED) is False


def test_an_attempt_at_an_unknown_tool_is_still_reported_as_an_attempt():
    """Nothing is recoverable, but the answer is still not an answer — the
    caller needs to know so it does not print the fabrication."""
    text = '```tool\n{"name": "list_files", "arguments": {"path": "data/"}}\n```'

    assert recover_tool_calls(text, REGISTERED) == []
    assert looks_like_a_tool_attempt(text, REGISTERED) is True


def test_a_malformed_block_is_an_attempt_but_not_a_call():
    text = '```tool\n{"name": "read_file", "arguments": {oops\n```'

    assert recover_tool_calls(text, REGISTERED) == []
    assert looks_like_a_tool_attempt(text, REGISTERED) is True


def test_empty_text_is_neither():
    assert recover_tool_calls("", REGISTERED) == []
    assert looks_like_a_tool_attempt("", REGISTERED) is False


def test_recovery_works_for_any_extension_without_wiring():
    """The generality requirement: a site's own extension tool is recoverable
    purely by being in the registry it was already added to."""
    site_tools = {"vcu_flow_loop_pump_status": object()}
    text = '```tool\n{"name": "vcu_flow_loop_pump_status", "arguments": {"loop": "A"}}\n```'

    calls = recover_tool_calls(text, site_tools)

    assert [c.name for c in calls] == ["vcu_flow_loop_pump_status"]
    assert calls[0].input == {"loop": "A"}
