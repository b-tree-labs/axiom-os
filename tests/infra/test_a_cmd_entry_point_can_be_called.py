# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every `cmd` a manifest declares must be callable the way `axi` calls it.

`axi lane` shipped, appeared in `axi --help`, registered its skills, and could
not be invoked at all: every verb ended at

    axi: command 'lane' failed: main() missing 2 required positional
    arguments: 'args' and 'ctx'

The entry point took `(args, ctx)` while the dispatcher calls `main(argv)`.
Nothing below that line was wrong — the dispatch was careful and the skills
were registered — which is why it survived review and a full test suite: the
extension's own tests call the dispatch function directly, with a context they
build themselves, so they never go through the door a person uses.

This checks the shape of that door, for every extension, without running
anything: a `cmd` entry must accept being called with a single optional
argument.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import tomllib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "axiom"


def _declared_commands() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for manifest in sorted(SRC.rglob("axiom-extension.toml")):
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            continue
        for provided in data.get("extension", {}).get("provides", []) or []:
            if provided.get("kind") == "cmd" and provided.get("entry"):
                out.append((provided.get("name", "?"), provided["entry"]))
    return out


def test_there_are_commands_to_check():
    """A scan that finds nothing passes for the wrong reason."""
    assert len(_declared_commands()) >= 5


@pytest.mark.parametrize(
    ("name", "entry"), _declared_commands(), ids=[n for n, _ in _declared_commands()]
)
def test_the_entry_point_accepts_an_argv(name, entry):
    module_path, _, attr = entry.partition(":")
    module = importlib.import_module(module_path)
    fn = getattr(module, attr, None)
    assert fn is not None, f"{entry} names nothing importable"

    sig = inspect.signature(fn)
    required = [
        p
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    assert not required, (
        f"`axi {name}` entry {entry}{sig} requires {[p.name for p in required]}. "
        "The dispatcher calls it as main(argv) with argv optional, so this "
        "command cannot be invoked at all — it will fail on every verb with "
        "'missing required positional arguments'."
    )


def _required_positionals(fn) -> list[str]:
    return [
        p.name
        for p in inspect.signature(fn).parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]


class TestTheCheckCanFail:
    """Sixty commands passing says nothing unless the check can say no."""

    def test_it_rejects_the_shape_that_shipped_broken(self):
        def main(args, ctx):  # the signature `axi lane` actually had
            return 0

        assert _required_positionals(main) == ["args", "ctx"]

    def test_it_accepts_the_shape_the_dispatcher_calls(self):
        def main(argv: list[str] | None = None) -> int:
            return 0

        assert _required_positionals(main) == []

    def test_a_keyword_only_argument_is_not_a_blocker(self):
        """`main(argv=None, *, quiet=False)` is callable as `main(argv)`."""

        def main(argv=None, *, quiet=False):
            return 0

        assert _required_positionals(main) == []
