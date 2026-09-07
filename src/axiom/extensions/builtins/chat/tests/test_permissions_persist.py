# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Cross-session persistence of per-tool permissions.

An ``A`` (Always allow) or ``D`` (Deny always) chosen at an approval prompt,
or a mode set via ``/permissions``, lands in
``$AXI_STATE_DIR/tool_permissions.json`` and is honoured by the next
session. Every test here points the state dir at ``tmp_path`` (the chat
conftest does it for all tests; these re-assert it) so nothing touches the
developer's real state tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.permissions import (
    ToolPermissions,
    format_permissions,
)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    return state


# ---------------------------------------------------------------------------
# ToolPermissions with an explicit path
# ---------------------------------------------------------------------------


def test_set_writes_json_and_fresh_instance_reads_it_back(tmp_path: Path):
    path = tmp_path / "perms.json"
    perms = ToolPermissions(path)
    perms.set("write_file", "allow")
    perms.set("email_send", "deny")

    assert json.loads(path.read_text()) == {"write_file": "allow", "email_send": "deny"}

    fresh = ToolPermissions(path)
    assert fresh.get("write_file") == "allow"
    assert fresh.get("email_send") == "deny"
    assert fresh.get("doc_publish") == "ask"


def test_constructing_with_path_does_not_create_the_file(tmp_path: Path):
    """Loading is read-only: a session that never persists leaves no file."""
    path = tmp_path / "perms.json"
    ToolPermissions(path)
    assert not path.exists()


def test_reset_tool_removes_key_on_disk(tmp_path: Path):
    path = tmp_path / "perms.json"
    perms = ToolPermissions(path)
    perms.set("write_file", "allow")
    perms.set("email_send", "deny")

    perms.reset("write_file")

    assert json.loads(path.read_text()) == {"email_send": "deny"}
    assert ToolPermissions(path).all() == {"email_send": "deny"}


def test_reset_all_empties_file(tmp_path: Path):
    path = tmp_path / "perms.json"
    perms = ToolPermissions(path)
    perms.set("write_file", "allow")

    perms.reset()

    assert json.loads(path.read_text()) == {}
    assert ToolPermissions(path).all() == {}


def test_set_creates_missing_parent_directories(tmp_path: Path):
    path = tmp_path / "nested" / "deeper" / "perms.json"
    ToolPermissions(path).set("write_file", "allow")
    assert json.loads(path.read_text()) == {"write_file": "allow"}


def test_write_leaves_no_temp_files_behind(tmp_path: Path):
    path = tmp_path / "perms.json"
    ToolPermissions(path).set("write_file", "allow")
    assert [p.name for p in tmp_path.iterdir()] == ["perms.json"]


def test_malformed_json_on_disk_loads_empty_without_raising(tmp_path: Path):
    path = tmp_path / "perms.json"
    path.write_text("{not json")

    perms = ToolPermissions(path)

    assert perms.all() == {}
    assert perms.get("write_file") == "ask"


def test_non_object_json_on_disk_loads_empty(tmp_path: Path):
    path = tmp_path / "perms.json"
    path.write_text('["write_file"]')
    assert ToolPermissions(path).all() == {}


def test_unknown_modes_on_disk_are_ignored(tmp_path: Path):
    path = tmp_path / "perms.json"
    path.write_text(
        json.dumps(
            {
                "write_file": "allow",
                "email_send": "bogus",
                "doc_publish": 42,
                "signal_ingest": "deny",
            }
        )
    )

    perms = ToolPermissions(path)

    assert perms.all() == {"write_file": "allow", "signal_ingest": "deny"}
    assert perms.get("email_send") == "ask"
    assert perms.get("doc_publish") == "ask"


def test_next_write_drops_ignored_entries(tmp_path: Path):
    """Once a corrupt entry is ignored it is not carried forward on disk."""
    path = tmp_path / "perms.json"
    path.write_text(json.dumps({"write_file": "allow", "email_send": "bogus"}))

    perms = ToolPermissions(path)
    perms.set("doc_publish", "deny")

    assert json.loads(path.read_text()) == {"write_file": "allow", "doc_publish": "deny"}


# ---------------------------------------------------------------------------
# No path: pure in-memory, nothing written anywhere
# ---------------------------------------------------------------------------


def test_no_path_writes_nothing(state_dir: Path, tmp_path: Path):
    perms = ToolPermissions()
    perms.set("write_file", "allow")
    perms.reset("write_file")
    perms.reset()

    assert perms.path is None
    assert not (state_dir / "tool_permissions.json").exists()
    assert list(tmp_path.rglob("tool_permissions*")) == []


# ---------------------------------------------------------------------------
# Default path under the user state dir
# ---------------------------------------------------------------------------


def test_default_path_lives_under_user_state_dir(state_dir: Path):
    assert ToolPermissions.default_path() == state_dir / "tool_permissions.json"


def test_load_default_round_trips_across_instances(state_dir: Path):
    first = ToolPermissions.load_default()
    assert first.path == state_dir / "tool_permissions.json"
    first.set("write_file", "allow")

    second = ToolPermissions.load_default()
    assert second.get("write_file") == "allow"
    assert json.loads((state_dir / "tool_permissions.json").read_text()) == {"write_file": "allow"}


def test_load_default_with_nothing_persisted_creates_no_file(state_dir: Path):
    perms = ToolPermissions.load_default()
    assert perms.all() == {}
    assert not (state_dir / "tool_permissions.json").exists()


# ---------------------------------------------------------------------------
# /permissions display
# ---------------------------------------------------------------------------


def test_format_mentions_storage_path_only_when_set(tmp_path: Path):
    path = tmp_path / "perms.json"

    with_path = ToolPermissions(path)
    with_path.set("write_file", "allow")
    assert str(path) in format_permissions(with_path)

    empty_with_path = ToolPermissions(path)
    empty_with_path.reset()
    assert str(path) in format_permissions(empty_with_path)

    in_memory = ToolPermissions()
    in_memory.set("write_file", "allow")
    assert str(path) not in format_permissions(in_memory)
    assert "stored" not in format_permissions(in_memory).lower()


def test_cmd_permissions_set_persists_to_disk(tmp_path: Path):
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.commands import cmd_permissions

    path = tmp_path / "perms.json"
    agent = SimpleNamespace(permissions=ToolPermissions(path))

    cmd_permissions(agent, ["set", "email_send", "deny"])
    assert json.loads(path.read_text()) == {"email_send": "deny"}

    cmd_permissions(agent, ["reset", "email_send"])
    assert json.loads(path.read_text()) == {}


# ---------------------------------------------------------------------------
# Agent path: an approval choice survives into a new agent
# ---------------------------------------------------------------------------


def _make_agent(**kwargs):
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.bus import EventBus
    from axiom.infra.gateway import Gateway
    from axiom.infra.orchestrator.session import Session

    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    return ChatAgent(gateway=gw, bus=EventBus(), session=Session(), **kwargs)


def _make_response(tool_name, tool_id="t1", file_path="/tmp/t.txt"):
    from axiom.infra.gateway import CompletionResponse, ToolUseBlock

    return CompletionResponse(
        text="",
        tool_use=[
            ToolUseBlock(
                tool_id=tool_id,
                name=tool_name,
                input={"file_path": file_path, "content": "x"},
            )
        ],
        provider="test",
        success=True,
    )


def test_agent_loads_default_permissions_from_state_dir(state_dir: Path):
    agent = _make_agent()
    assert agent.permissions.path == state_dir / "tool_permissions.json"


def test_agent_accepts_injected_permissions(state_dir: Path):
    injected = ToolPermissions()
    agent = _make_agent(permissions=injected)
    assert agent.permissions is injected
    assert agent.permissions.path is None


def test_always_allow_persists_to_a_new_agent(state_dir: Path, tmp_path: Path):
    agent = _make_agent()
    render = MagicMock()
    render.render_approval_prompt.return_value = "A"
    agent.set_render_provider(render)

    agent._process_tool_calls(_make_response("write_file", "t1", str(tmp_path / "a.txt")))
    assert render.render_approval_prompt.call_count == 1

    on_disk = json.loads((state_dir / "tool_permissions.json").read_text())
    assert on_disk == {"write_file": "allow"}

    # A brand-new agent (next session) honours the choice without prompting.
    second = _make_agent()
    assert second.permissions.get("write_file") == "allow"
    render2 = MagicMock()
    render2.render_approval_prompt.return_value = "should_not_be_called"
    second.set_render_provider(render2)
    second._process_tool_calls(_make_response("write_file", "t2", str(tmp_path / "b.txt")))
    assert render2.render_approval_prompt.call_count == 0
    assert (tmp_path / "b.txt").read_text() == "x"


def test_deny_always_persists_to_a_new_agent(state_dir: Path, tmp_path: Path):
    agent = _make_agent()
    render = MagicMock()
    render.render_approval_prompt.return_value = "D"
    agent.set_render_provider(render)

    agent._process_tool_calls(_make_response("write_file", "t1", str(tmp_path / "a.txt")))
    assert not (tmp_path / "a.txt").exists()

    on_disk = json.loads((state_dir / "tool_permissions.json").read_text())
    assert on_disk == {"write_file": "deny"}

    second = _make_agent()
    assert second.permissions.get("write_file") == "deny"
    render2 = MagicMock()
    render2.render_approval_prompt.return_value = "a"
    second.set_render_provider(render2)
    results = second._process_tool_calls(
        _make_response("write_file", "t2", str(tmp_path / "b.txt"))
    )
    assert render2.render_approval_prompt.call_count == 0
    assert not (tmp_path / "b.txt").exists()
    assert any("error" in r[2] for r in results)


def test_plain_approve_does_not_persist(state_dir: Path, tmp_path: Path):
    """A one-off ``a`` never touches disk; only ``A``/``D`` persist."""
    agent = _make_agent()
    render = MagicMock()
    render.render_approval_prompt.return_value = "a"
    agent.set_render_provider(render)

    agent._process_tool_calls(_make_response("write_file", "t1", str(tmp_path / "a.txt")))

    assert not (state_dir / "tool_permissions.json").exists()
    assert _make_agent().permissions.all() == {}
