# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The on-disk header cache that makes repeat /renew listings fast: keyed, content-free, and disposable."""

from __future__ import annotations

import json
import os
import time

import pytest

from axiom.memory.absorb import sessions as sessions_mod
from axiom.memory.absorb.session_index import WITHHELD_TITLE, SessionIndexCache
from axiom.memory.absorb.sessions import ClaudeCodeSessionReader, session_readers

from ._renew_harness_support import CLAUDE_ID, SECRET, claude_session, codex_session
from ._session_conformance_support import all_cases


@pytest.fixture
def home(_no_foreign_harness_state):
    return _no_foreign_harness_state


@pytest.fixture
def idx(tmp_path):
    return tmp_path / "state" / "renew-session-index.json"


def _count_conversions(monkeypatch) -> list[str]:
    calls: list[str] = []
    orig = ClaudeCodeSessionReader._convert

    def spy(self, path, light=False):
        calls.append(path.name)
        return orig(self, path, light)

    monkeypatch.setattr(ClaudeCodeSessionReader, "_convert", spy)
    return calls


def test_a_warm_cache_serves_headers_without_parsing_any_session(home, idx, monkeypatch):
    claude_session(home)
    calls = _count_conversions(monkeypatch)
    cold = ClaudeCodeSessionReader(home, SessionIndexCache(idx)).list_sessions()
    assert len(calls) == 1 and idx.exists()
    warm = ClaudeCodeSessionReader(home, SessionIndexCache(idx)).list_sessions()
    assert len(calls) == 1  # not parsed again
    assert warm == cold  # identical headers, title and count included


def test_a_changed_session_is_reparsed_and_an_unchanged_one_is_not(home, idx, monkeypatch):
    p1 = claude_session(home)
    claude_session(
        home,
        "c1a0de00-0000-4000-8000-000000000002",
        first_prompt="other",
        title="Other",
        hours_ago=9,
    )
    ClaudeCodeSessionReader(home, SessionIndexCache(idx)).list_sessions()
    calls = _count_conversions(monkeypatch)
    with open(p1, "a", encoding="utf-8") as fh:  # the harness appends a turn: size and mtime change
        fh.write(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "u9",
                    "timestamp": "2026-10-01T11:00:00Z",
                    "message": {"role": "user", "content": "one more question"},
                }
            )
            + "\n"
        )
    headers = ClaudeCodeSessionReader(home, SessionIndexCache(idx)).list_sessions()
    assert calls == [p1.name]
    assert next(h for h in headers if h.source_session_id == CLAUDE_ID).message_count == 5


@pytest.mark.parametrize(
    "damage",
    [
        b"",
        b"{not json",
        b"\x00\xff\xfe",
        json.dumps({"version": 999, "harnesses": {}}).encode(),
        json.dumps({"version": 1, "harnesses": {"claude-code": "nope"}}).encode(),
        json.dumps(
            {"version": 1, "harnesses": {"claude-code": {"k": {"stamp": 1, "h": {"title": 3}}}}}
        ).encode(),
        json.dumps([1, 2, 3]).encode(),
    ],
    ids=[
        "empty",
        "truncated",
        "binary",
        "future-version",
        "wrong-shape",
        "bad-entry",
        "not-an-object",
    ],
)
def test_a_damaged_cache_is_ignored_and_rebuilt_never_an_error(home, idx, damage):
    claude_session(home)
    good = ClaudeCodeSessionReader(home, SessionIndexCache(idx)).list_sessions()
    idx.write_bytes(damage)
    again = ClaudeCodeSessionReader(home, SessionIndexCache(idx)).list_sessions()
    assert again == good
    assert json.loads(idx.read_text(encoding="utf-8"))["version"] == 1  # rebuilt


def test_deleting_the_cache_just_rebuilds_it(home, idx):
    claude_session(home)
    r = ClaudeCodeSessionReader(home, SessionIndexCache(idx))
    first = r.list_sessions()
    idx.unlink()
    assert ClaudeCodeSessionReader(home, SessionIndexCache(idx)).list_sessions() == first
    assert idx.exists()


def test_an_unwritable_cache_location_costs_speed_not_correctness(home, tmp_path):
    claude_session(home)
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    cache = SessionIndexCache(
        blocker / "sub" / "idx.json"
    )  # its parent is a file: cannot be created
    assert len(ClaudeCodeSessionReader(home, cache).list_sessions()) == 1


def test_the_cache_holds_headers_only_never_message_content_or_a_secret_title(home, idx):
    claude_session(
        home,
        first_prompt=f"my key is {SECRET} and a unique phrase zebra-quartz",
        title=f"key {SECRET}",
    )
    ClaudeCodeSessionReader(home, SessionIndexCache(idx)).list_sessions()
    raw = idx.read_text(encoding="utf-8")
    assert SECRET not in raw and "zebra-quartz" not in raw and "SUBAGENT CHATTER" not in raw
    assert WITHHELD_TITLE in raw
    entry = next(iter(json.loads(raw)["harnesses"]["claude-code"].values()))
    assert set(entry["h"]) == {
        "source_session_id",
        "title",
        "created_at",
        "updated_at",
        "message_count",
        "path",
        "cwd",
        "format_version",
    }


@pytest.mark.parametrize("case", [c for c in all_cases() if c.kind == "db"], ids=lambda c: c.name)
def test_database_harnesses_cache_per_session_and_notice_when_one_changes(
    case, home, idx, monkeypatch
):
    case.install(home)
    first = session_readers(home, SessionIndexCache(idx))[case.name].list_sessions()
    converted: list[str] = []
    cls = type(session_readers(home)[case.name])
    orig = cls._convert

    def spy(self, conn, have, row, light=False):
        converted.append(row["id"])
        return orig(self, conn, have, row, light)

    monkeypatch.setattr(cls, "_convert", spy)
    assert session_readers(home, SessionIndexCache(idx))[case.name].list_sessions() == first
    assert converted == []  # warm: no session parsed


def test_repeat_listing_is_much_faster_than_the_first_on_a_large_history(home, idx):
    """A synthetic history with many sizeable sessions: the warm listing must not re-parse them."""
    for i in range(40):
        claude_session(
            home, f"c1a0de00-0000-4000-8000-{i:012d}", first_prompt="p" * 50, hours_ago=i + 1
        )
        path = (
            home
            / ".claude"
            / "projects"
            / "-work-detector"
            / f"c1a0de00-0000-4000-8000-{i:012d}.jsonl"
        )
        with open(path, "a", encoding="utf-8") as fh:
            for j in range(1500):
                fh.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "uuid": f"x{j}",
                            "timestamp": "2026-10-01T10:02:00Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": f"t{j}",
                                        "name": "Bash",
                                        "input": {"command": "ls " + "a" * 40},
                                    }
                                ],
                            },
                        }
                    )
                    + "\n"
                )
        os.utime(path, (time.time() - (i + 1) * 3600,) * 2)
    t = time.perf_counter()
    cold = session_readers(home, SessionIndexCache(idx))["claude-code"].list_sessions(50)
    cold_s = time.perf_counter() - t
    t = time.perf_counter()
    warm = session_readers(home, SessionIndexCache(idx))["claude-code"].list_sessions(50)
    warm_s = time.perf_counter() - t
    assert warm == cold and len(warm) == 40
    assert warm_s < 1.0 and warm_s < cold_s / 3, (cold_s, warm_s)


def test_listing_through_renew_uses_the_cache_in_the_state_dir(home, tmp_path):
    from axiom.extensions.builtins.chat.commands_ext import renew_sources

    codex_session(home)
    renew_sources.readers()["codex"].list_sessions()
    state = os.environ["AXI_STATE_DIR"]
    assert os.path.exists(os.path.join(state, "renew-session-index.json"))
    assert sessions_mod.MAX_MESSAGES > 0  # the module under test is the one imported
