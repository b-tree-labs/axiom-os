# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Two bridge faults found reviewing 0.70.46 (#2229, #2250).

- An OpenAI-format stream ended with a ``usage`` object that held only
  ``prompt_tokens_details``. A client that reads ``usage.prompt_tokens`` when
  ``usage`` is present found nothing there.
- With no session header and no ``metadata.user_id`` the conversation id is a
  hash of the stable prefix. The first message carries the harness's cache
  marker on turn 1 and not on turn 2, so the id changed after the first turn.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from axiom.llm.anthropic_ingress import (
    anthropic_to_gateway,
    harness_session_id,
    stream_to_openai_sse,
)


def _events(chunks) -> list[dict]:
    out = "".join(stream_to_openai_sse(chunks, "m"))
    return [json.loads(e[6:]) for e in out.split("\n\n") if e.startswith("data: {")]


def _usage(**kw):
    base = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    return SimpleNamespace(type="usage", **{**base, **kw})


def test_a_stream_with_usage_ends_with_the_whole_usage_object():
    chunks = [
        _usage(input_tokens=5, cache_read_tokens=100),
        SimpleNamespace(type="text", text="ok"),
        _usage(output_tokens=2),
        SimpleNamespace(type="done"),
    ]
    usage = _events(chunks)[-1]["usage"]
    assert usage["prompt_tokens"] == 5
    assert usage["completion_tokens"] == 2
    assert usage["total_tokens"] == 7
    assert usage["prompt_tokens_details"] == {"cached_tokens": 100}


def test_a_stream_that_reported_no_usage_says_nothing_about_usage():
    # Before #2229 the stream carried no usage; a made-up zero is worse than none.
    chunks = [SimpleNamespace(type="text", text="ok"), SimpleNamespace(type="done")]
    events = _events(chunks)
    assert all("usage" not in e for e in events)
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


_SYSTEM = [{"type": "text", "text": "You are X", "cache_control": {"type": "ephemeral"}}]
_MARK = {"cache_control": {"type": "ephemeral"}}


def _turn(*messages, tools=None):
    body = {"system": _SYSTEM, "messages": list(messages)}
    if tools:
        body["tools"] = tools
    return harness_session_id({}, body, anthropic_to_gateway(body))


def _user(text, marked=False):
    return {
        "role": "user",
        "content": [{"type": "text", "text": text, **(_MARK if marked else {})}],
    }


def test_the_prefix_id_is_the_same_when_the_cache_marker_moves_on():
    first = _turn(_user("hi", marked=True))
    second = _turn(
        _user("hi"), {"role": "assistant", "content": "hello"}, _user("more", marked=True)
    )
    assert first == second


def test_the_prefix_id_ignores_a_marker_on_a_tool():
    tool = {"name": "read", "description": "read a file", "input_schema": {"type": "object"}}
    plain = _turn(_user("hi"), tools=[tool])
    marked = _turn(_user("hi"), tools=[{**tool, **_MARK}])
    assert plain == marked


def test_the_prefix_id_still_tells_two_openers_apart():
    assert _turn(_user("hi")) != _turn(_user("hello"))
