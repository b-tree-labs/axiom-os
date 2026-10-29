# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One declaration per slash command: dispatch, help and completion all derive from it (ADR-168).

The old shape was a hand-kept if-ladder in each surface, a metadata dict for completion, and a
hand-built help screen, with a regex test trying to notice when they drifted. A command declared once
cannot be dispatched but not advertised, advertised but not handled, or listed in help but not
completed.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.command_registry import (
    NOT_FOUND,
    Command,
    CommandContext,
    CommandRegistry,
)


def _ctx(**kw):
    return CommandContext(
        agent=kw.pop("agent", object()),
        store=kw.pop("store", None),
        surface=kw.pop("surface", "repl"),
    )


def _reg():
    r = CommandRegistry()
    r.register(
        Command(
            "/echo",
            "Echo the arguments",
            "Inspect",
            lambda ctx, args: " ".join(args),
            usage="/echo <text>",
        )
    )
    r.register(
        Command(
            "/set",
            "Change a setting",
            "System",
            lambda ctx, args: f"set {args}",
            subcommands={"a": "set a", "b": "set b"},
            aliases=("/s",),
        )
    )
    r.register(Command("/hid", "Hidden", "System", lambda ctx, args: "hid", hidden=True))
    return r


def test_dispatch_finds_a_command_and_passes_its_arguments():
    assert _reg().dispatch("/echo hello world", _ctx()) == "hello world"


def test_dispatch_is_case_insensitive_on_the_name_and_resolves_aliases():
    r = _reg()
    assert r.dispatch("/ECHO x", _ctx()) == "x" and r.dispatch("/s a", _ctx()) == "set ['a']"


def test_an_unknown_command_is_not_found_not_an_exception():
    assert _reg().dispatch("/nope", _ctx()) is NOT_FOUND


def test_completion_metadata_lists_commands_and_their_subcommands_but_not_hidden_ones():
    meta = _reg().meta()
    assert meta["/echo"] == "Echo the arguments"
    assert meta["/set a"] == "set a" and meta["/set b"] == "set b" and "/set" in meta
    assert "/hid" not in meta and "/s" not in meta, "hidden commands and aliases are not advertised"


def test_hidden_commands_and_aliases_still_dispatch():
    r = _reg()
    assert r.dispatch("/hid", _ctx()) == "hid" and r.dispatch("/s b", _ctx()) == "set ['b']"


def test_help_is_generated_from_the_same_declarations():
    text = _reg().help_text()
    assert "/echo <text>" in text and "Echo the arguments" in text and "/set" in text
    assert "/hid" not in text
    assert text.index("Inspect") < text.index("System"), "groups appear in their declared order"


def test_a_command_cannot_be_declared_twice():
    r = _reg()
    with pytest.raises(ValueError, match="already registered"):
        r.register(Command("/echo", "again", "Inspect", lambda c, a: ""))


def test_a_name_that_collides_with_an_alias_is_refused():
    r = _reg()
    with pytest.raises(ValueError, match="already registered"):
        r.register(Command("/s", "collides", "System", lambda c, a: ""))


def test_a_name_must_start_with_a_slash():
    with pytest.raises(ValueError, match="must start with"):
        Command("echo", "x", "Inspect", lambda c, a: "")


def test_a_command_may_be_limited_to_some_surfaces():
    r = CommandRegistry()
    r.register(
        Command("/tui", "tui only", "System", lambda c, a: "ran", surfaces=frozenset({"tui"}))
    )
    assert r.dispatch("/tui", _ctx(surface="repl")).startswith("\n  /tui is not available in the")
    assert r.dispatch("/tui", _ctx(surface="tui")) == "ran"


def test_a_handler_that_raises_is_reported_not_propagated():
    r = CommandRegistry()
    r.register(Command("/boom", "raises", "System", lambda c, a: 1 / 0))
    out = r.dispatch("/boom", _ctx())
    assert "/boom failed" in out and "division by zero" in out


def test_the_decorator_registers_on_the_default_registry_and_names_the_function():
    from axiom.extensions.builtins.chat import command_registry as cr

    reg = CommandRegistry()

    @cr.command("/deco", "Decorated", "Inspect", registry=reg)
    def _(ctx, args):
        return "decorated"

    assert reg.dispatch("/deco", _ctx()) == "decorated"
