# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi pub`` CLI vocabulary — canonical verb set + deprecation aliases.

Pins the v0.30 ``axi pub`` vocabulary. Each verb must reach a registered
skill (per ADR-056 once M2 lands) and the deprecation aliases must keep
the old names working through v0.30.x with a visible warning.

The canonical vocabulary was chosen 2026-06-01 with these constraints:

- **Imperative when an action** (``draft``, ``publish``, ``review``,
  ``upload``, ``scan``, ``onboard``, ``watch``, ``assemble``, ``pull``)
- **Resource-named when a query** (``status``, ``providers``,
  ``overview``, ``diff``)
- **No double-verb collisions** with the namespace — the namespace
  ``pub`` is short enough that ``axi pub publish`` reads as
  "publishing.publish" not "publish-publish"
- **One vocabulary across help text + parser + handler dispatch** —
  this test is the single source of truth
"""

from __future__ import annotations

import argparse

import pytest


# ---------------------------------------------------------------------------
# Canonical vocabulary (v0.30)
# ---------------------------------------------------------------------------


CANONICAL_VERBS: frozenset[str] = frozenset({
    # Action verbs — produce or move artifacts
    "draft",        # render local-only (was: generate)
    "publish",      # generate + upload + notify
    "review",       # interactive HITL
    "pull",         # fetch external version → local
    "scan",         # scan source dirs vs manifest
    "onboard",      # register a doc into the manifest
    "watch",        # daemonize — auto-publish on save
    "push",         # storage upload only (no notify)
    "assemble",     # multi-section assembly from .compile.yaml
    "check-links",  # verify cross-doc links resolve
    "do",           # execute a named standard bundle (ADR-058)

    # Query verbs — show state, no side effects
    "status",
    "providers",
    "overview",
    "diff",
    "standards",    # list PRESS standards bundles (ADR-058)
})


# Deprecated → canonical (kept for one minor; removed in v0.31).
# Each alias must dispatch to the same handler as its canonical pair AND
# emit a deprecation notice on stderr per the verb-migration discipline
# memory.
DEPRECATED_ALIASES: dict[str, str] = {
    "generate": "draft",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Import the live CLI parser builder."""
    from axiom.extensions.builtins.publishing import cli as pub_cli

    parser = argparse.ArgumentParser(prog="axi pub")
    pub_cli.build_argparse(parser)
    return parser


def _parser_subverbs(parser: argparse.ArgumentParser) -> set[str]:
    """Extract the sub-parser choices."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


class TestCanonicalVocabulary:
    def test_every_canonical_verb_is_in_the_parser(self):
        verbs = _parser_subverbs(_build_parser())
        missing = CANONICAL_VERBS - verbs
        assert not missing, (
            f"canonical verbs missing from CLI parser: {sorted(missing)}"
        )

    def test_parser_has_no_undeclared_verbs(self):
        """Every parser verb must be either canonical or a declared alias."""
        verbs = _parser_subverbs(_build_parser())
        declared = CANONICAL_VERBS | set(DEPRECATED_ALIASES)
        undeclared = verbs - declared
        assert not undeclared, (
            f"parser exposes verbs not in the canonical+alias set: "
            f"{sorted(undeclared)} — either add to CANONICAL_VERBS or "
            f"to DEPRECATED_ALIASES"
        )

    @pytest.mark.parametrize("verb", sorted(CANONICAL_VERBS))
    def test_each_canonical_verb_has_help_text(self, verb: str):
        parser = _build_parser()
        sub = next(
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        choice = sub.choices.get(verb)
        assert choice is not None, f"verb {verb!r} missing from parser"
        # Help is set on the subparser, not the choice. The fixture
        # action's `choices_actions` list carries the help string.
        help_strings = {
            a.dest: a.help for a in sub._choices_actions
        }
        # The recorded help may be None for some legacy verbs, but the
        # canonical ones must have a one-line description.
        recorded_help = help_strings.get(verb)
        assert recorded_help, (
            f"verb {verb!r} has no help text — add `help=...` to its "
            f"`subparsers.add_parser(...)` call"
        )


class TestDeprecatedAliases:
    @pytest.mark.parametrize("alias,canonical", sorted(DEPRECATED_ALIASES.items()))
    def test_alias_is_still_in_parser(self, alias: str, canonical: str):
        verbs = _parser_subverbs(_build_parser())
        assert alias in verbs, (
            f"deprecated alias {alias!r} (→ {canonical}) removed from "
            f"parser before v0.31; either delete the alias entry or "
            f"restore the subparser"
        )

    @pytest.mark.parametrize("alias,canonical", sorted(DEPRECATED_ALIASES.items()))
    def test_alias_dispatches_to_canonical_handler(
        self, alias: str, canonical: str
    ):
        """Invoking the alias on a tmp source must reach the same handler
        the canonical verb would, AND emit a deprecation notice."""
        from axiom.extensions.builtins.publishing import cli as pub_cli

        # Verify the dispatch map (or its equivalent) routes alias →
        # canonical handler. The dispatch lives in ``main`` as an
        # if/elif chain; we test by introspecting the resolver.
        resolved = pub_cli.resolve_verb_handler(alias)
        canonical_resolved = pub_cli.resolve_verb_handler(canonical)
        assert resolved is canonical_resolved, (
            f"alias {alias!r} resolves to {resolved!r} but the canonical "
            f"{canonical!r} resolves to {canonical_resolved!r}; they must "
            f"be the same handler"
        )

    @pytest.mark.parametrize("alias", sorted(DEPRECATED_ALIASES))
    def test_alias_emits_deprecation_warning(
        self, alias: str, capsys: pytest.CaptureFixture
    ):
        from axiom.extensions.builtins.publishing import cli as pub_cli

        pub_cli.emit_deprecation_warning_if_alias(alias)
        captured = capsys.readouterr()
        assert "deprecated" in captured.err.lower()
        assert alias in captured.err
        assert DEPRECATED_ALIASES[alias] in captured.err


class TestVocabularyDocumentation:
    def test_module_docstring_lists_every_canonical_verb(self):
        from axiom.extensions.builtins.publishing import cli as pub_cli

        doc = pub_cli.__doc__ or ""
        # We don't enforce exact ordering, just that each canonical verb
        # appears at least once in the module docstring.
        missing = [
            v for v in CANONICAL_VERBS
            if f" {v} " not in doc and f" {v}\n" not in doc
                and f" {v}<" not in doc and f"`{v}`" not in doc
                and f"axi pub {v}" not in doc
        ]
        assert not missing, (
            f"module docstring doesn't mention canonical verb(s): "
            f"{sorted(missing)} — add to the cli.py header"
        )
