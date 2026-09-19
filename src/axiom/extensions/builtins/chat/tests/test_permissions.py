# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for per-tool permission mode (allow / ask / deny).

Closes the parity-doc gap: 'Fine-grained per-tool permissions
(allow/deny/ask patterns per principal)'. The existing ApprovalGate
classifies actions as READ (auto-approve) or WRITE (ask), but a user's
'Always allow' choice from the prompt did not persist beyond the single
call. ToolPermissions persists the choice for the rest of the session
and is consulted before the approval gate.
"""

from __future__ import annotations


def test_default_mode_is_ask():
    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    perms = ToolPermissions()
    assert perms.get("write_file") == "ask"
    assert perms.get("anything_else") == "ask"


def test_set_allow_sticks():
    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    perms = ToolPermissions()
    perms.set("write_file", "allow")
    assert perms.get("write_file") == "allow"
    # Other tools stay at ask.
    assert perms.get("doc_publish") == "ask"


def test_set_deny_sticks():
    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    perms = ToolPermissions()
    perms.set("email_send", "deny")
    assert perms.get("email_send") == "deny"


def test_invalid_mode_raises():
    import pytest

    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    perms = ToolPermissions()
    with pytest.raises(ValueError):
        perms.set("write_file", "bogus")  # type: ignore[arg-type]


def test_reset_specific_tool():
    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    perms = ToolPermissions()
    perms.set("write_file", "allow")
    perms.set("email_send", "deny")
    perms.reset("write_file")
    assert perms.get("write_file") == "ask"
    # Other tool unaffected.
    assert perms.get("email_send") == "deny"


def test_reset_all():
    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    perms = ToolPermissions()
    perms.set("write_file", "allow")
    perms.set("email_send", "deny")
    perms.reset()
    assert perms.get("write_file") == "ask"
    assert perms.get("email_send") == "ask"
    assert perms.all() == {}


def test_all_returns_explicit_settings_only():
    """all() is the persisted state — tools never explicitly set should
    not appear (otherwise the /permissions display becomes unbounded)."""
    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    perms = ToolPermissions()
    perms.set("write_file", "allow")
    assert perms.all() == {"write_file": "allow"}


def test_format_for_display():
    from axiom.extensions.builtins.chat.permissions import (
        ToolPermissions,
        format_permissions,
    )

    perms = ToolPermissions()
    perms.set("write_file", "allow")
    perms.set("email_send", "deny")
    out = format_permissions(perms)
    assert "write_file" in out
    assert "allow" in out
    assert "email_send" in out
    assert "deny" in out


def test_format_when_empty():
    from axiom.extensions.builtins.chat.permissions import (
        ToolPermissions,
        format_permissions,
    )

    out = format_permissions(ToolPermissions())
    # Should communicate "all tools default to ask" — not silently empty.
    assert "ask" in out.lower() or "default" in out.lower()


def test_chat_agent_has_permissions_attribute():
    """ChatAgent.__init__ wires a fresh ToolPermissions instance."""
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    agent = ChatAgent()
    assert isinstance(agent.permissions, ToolPermissions)
    # Default — no overrides yet.
    assert agent.permissions.all() == {}


def test_cmd_permissions_view():
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.commands import cmd_permissions
    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    perms = ToolPermissions()
    perms.set("write_file", "allow")
    agent = SimpleNamespace(permissions=perms)
    out = cmd_permissions(agent, [])
    assert "write_file" in out
    assert "allow" in out


def test_cmd_permissions_set():
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.commands import cmd_permissions
    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    agent = SimpleNamespace(permissions=ToolPermissions())
    out = cmd_permissions(agent, ["set", "email_send", "deny"])
    assert "email_send" in out
    assert "deny" in out
    assert agent.permissions.get("email_send") == "deny"


def test_cmd_permissions_set_invalid_mode():
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.commands import cmd_permissions
    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    agent = SimpleNamespace(permissions=ToolPermissions())
    out = cmd_permissions(agent, ["set", "write_file", "bogus"])
    assert "unknown permission mode" in out.lower()
    # Permission was NOT applied.
    assert agent.permissions.get("write_file") == "ask"


def test_cmd_permissions_reset_specific():
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.commands import cmd_permissions
    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    perms = ToolPermissions()
    perms.set("write_file", "allow")
    perms.set("email_send", "deny")
    agent = SimpleNamespace(permissions=perms)
    cmd_permissions(agent, ["reset", "write_file"])
    assert agent.permissions.get("write_file") == "ask"
    assert agent.permissions.get("email_send") == "deny"


def test_cmd_permissions_reset_all():
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.commands import cmd_permissions
    from axiom.extensions.builtins.chat.permissions import ToolPermissions

    perms = ToolPermissions()
    perms.set("write_file", "allow")
    perms.set("email_send", "deny")
    agent = SimpleNamespace(permissions=perms)
    cmd_permissions(agent, ["reset"])
    assert agent.permissions.all() == {}
