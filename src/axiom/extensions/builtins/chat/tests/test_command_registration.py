# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A slash command is declared once; it is therefore discoverable by construction (ADR-168).

History: Ben, after `/alerts` shipped: "how do we ensure new additions get picked up properly in the
future?" Adding a command used to touch three places (the dispatcher, the help listing, completion) and a
regex test tried to notice when they disagreed. The registry removes the disagreement: dispatch, help and
completion all derive from one declaration. What is left to test is the property, not a scan for drift.
The one scan that remains covers commands a surface INTERCEPTS before the registry (the TUI's pickers),
because those are the only handlers the registry does not see.
"""

from __future__ import annotations

import re
from pathlib import Path

from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import NOT_FOUND, REGISTRY, CommandContext
from axiom.extensions.builtins.chat.commands import CHAT_META_COMMANDS, cmd_help, get_slash_commands

_CHAT = Path(__file__).resolve().parents[1]
ensure_loaded()


def test_the_registry_is_not_empty_and_the_metadata_view_reads_it_live():
    """Guards the guard: an empty registry would make every property below vacuously true."""
    assert len(REGISTRY.commands()) >= 20
    assert set(CHAT_META_COMMANDS) >= {c.name for c in REGISTRY.commands()}


def test_every_advertised_command_dispatches():
    ctx = CommandContext(agent=object(), store=None, surface="headless")
    for c in REGISTRY.commands():
        assert REGISTRY.get(c.name) is c
        # Dispatch with a context no handler can use: a registered command answers (even with an error
        # string), never NOT_FOUND. That is the "advertised but not handled" failure, impossible by design.
        assert REGISTRY.dispatch(c.name, ctx) is not NOT_FOUND, c.name


def test_every_registered_command_is_in_help_and_completion_unless_declared_hidden():
    plain = re.sub(r"\x1b\[[0-9;]*m", "", cmd_help())
    completion = get_slash_commands()
    for c in REGISTRY.commands(include_hidden=True):
        if c.hidden:
            assert c.name not in completion, f"{c.name} is hidden but is advertised"
            continue
        if c.requires == "admin":
            # Listed only to an administrator (see test_rag_and_trace_cmds); the default viewer is not one.
            assert c.name not in completion and c.name not in plain, f"{c.name} is admin-only but is advertised"
            continue
        assert c.name in plain, f"{c.name} is registered but missing from /help"
        assert c.name in completion, f"{c.name} is registered but does not complete"


def test_aliases_work_but_are_never_advertised():
    plain = re.sub(r"\x1b\[[0-9;]*m", "", cmd_help())
    completion = get_slash_commands()
    ctx = CommandContext(agent=object(), store=None, surface="headless")
    for c in REGISTRY.commands(include_hidden=True):
        for alias in c.aliases:
            assert REGISTRY.get(alias) is c
            assert REGISTRY.dispatch(alias, ctx) is not NOT_FOUND
            assert alias not in completion and alias not in plain.split(), (
                f"alias {alias} is advertised"
            )


def test_every_advertised_command_has_a_description():
    missing = [c for c, desc in CHAT_META_COMMANDS.items() if not (desc or "").strip()]
    assert missing == []


def test_the_description_column_starts_in_one_place():
    plain = re.sub(r"\x1b\[[0-9;]*m", "", cmd_help())
    starts = set()
    for line in plain.splitlines():
        m = re.match(r"^  (/\S+(?: [^ ]+)*?)( {2,})(\S)", line)
        if m:
            starts.add(len(m.group(1)) + len(m.group(2)) + 2)
    assert len(starts) == 1, f"descriptions start at columns {sorted(starts)}"


def test_the_help_groups_follow_the_declared_order():
    plain = re.sub(r"\x1b\[[0-9;]*m", "", cmd_help())
    order = [g for g in REGISTRY._group_order if g in plain]
    positions = [plain.index(g) for g in order]
    assert positions == sorted(positions)


def test_a_command_a_surface_intercepts_is_still_registered():
    """The TUI handles a few commands with pickers before the registry runs. They must still be declared,
    or they would not complete."""
    source = (_CHAT / "fullscreen.py").read_text()
    intercepted = set(re.findall(r'cmd == "(/[a-z]+)"', source))
    for group in re.findall(r"cmd in \(([^)]*)\)", source):
        intercepted |= set(re.findall(r'"(/[a-z]+)"', group))
    assert len(intercepted) >= 3, intercepted
    unregistered = {c for c in intercepted if REGISTRY.get(c) is None}
    assert unregistered == set(), (
        f"{sorted(unregistered)} are intercepted by the TUI but not registered"
    )
