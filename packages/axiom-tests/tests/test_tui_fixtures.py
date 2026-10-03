# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for the headless prompt_toolkit harness and key-binding lint."""

from __future__ import annotations

import pytest

pytest.importorskip("prompt_toolkit")

from prompt_toolkit.application import Application  # noqa: E402
from prompt_toolkit.filters import Condition  # noqa: E402
from prompt_toolkit.key_binding import KeyBindings  # noqa: E402
from prompt_toolkit.layout import Layout, Window  # noqa: E402

from axiom_tests.fixtures.tui import (  # noqa: E402
    BindingConflict,
    find_binding_conflicts,
    headless_terminal,
    key_sequence,
)


def _app(kb: KeyBindings | None = None) -> Application:
    return Application(layout=Layout(Window()), key_bindings=kb)


class TestHeadlessTerminal:
    def test_reports_requested_size_and_resizes(self):
        term = headless_terminal(_app(), columns=80, rows=24)
        try:
            size = term.app.output.get_size()
            assert (size.columns, size.rows) == (80, 24)
            term.resize(200, 50)
            size = term.app.output.get_size()
            assert (size.columns, size.rows) == (200, 50)
            assert (term.columns, term.rows) == (200, 50)
        finally:
            term.close()

    def test_factory_fixture_closes_at_teardown(self, headless_terminal_factory):
        term = headless_terminal_factory(_app(), columns=100, rows=30)
        assert term.app.output.get_size().columns == 100
        # teardown closes the pipe; nothing to assert beyond not leaking


class TestKeySequence:
    def test_normalises_enum_and_char_keys(self):
        kb = KeyBindings()

        @kb.add("c-c")
        def _cc(event):  # pragma: no cover - handler body irrelevant
            pass

        @kb.add("a", "b")
        def _ab(event):  # pragma: no cover
            pass

        seqs = [key_sequence(b) for b in kb.bindings]
        assert seqs == [("c-c",), ("a", "b")]


class TestFindBindingConflicts:
    def test_clean_table_reports_nothing(self):
        kb = KeyBindings()

        @kb.add("c-c")
        def _one(event):  # pragma: no cover
            pass

        @kb.add("c-d")
        def _two(event):  # pragma: no cover
            pass

        assert find_binding_conflicts(kb) == []

    def test_unconditional_duplicates_are_a_shadow(self):
        kb = KeyBindings()

        @kb.add("c-x")
        def _first(event):  # pragma: no cover
            pass

        @kb.add("c-x")
        def _second(event):  # pragma: no cover
            pass

        found = find_binding_conflicts(kb)
        assert found == [BindingConflict("shadow", ("c-x",), ("_first", "_second"), "")]
        assert "shadowed by _second" in found[0].describe()

    def test_filter_disambiguated_pair_is_not_a_conflict(self):
        state = {"picker": False}
        kb = KeyBindings()

        @kb.add("enter", filter=Condition(lambda: state["picker"]))
        def _pick(event):  # pragma: no cover
            pass

        @kb.add("enter", filter=Condition(lambda: not state["picker"]))
        def _submit(event):  # pragma: no cover
            pass

        states = {
            "picker closed": lambda: state.__setitem__("picker", False),
            "picker open": lambda: state.__setitem__("picker", True),
        }
        assert find_binding_conflicts(kb, states) == []

    def test_shadow_is_reported_in_the_state_where_both_are_live(self):
        state = {"picker": False}
        kb = KeyBindings()

        @kb.add("s-up", filter=Condition(lambda: not state["picker"]))
        def _select_up(event):  # pragma: no cover
            pass

        @kb.add("s-up")
        def _scroll_up(event):  # pragma: no cover
            pass

        states = {
            "picker closed": lambda: state.__setitem__("picker", False),
            "picker open": lambda: state.__setitem__("picker", True),
        }
        found = find_binding_conflicts(kb, states)
        assert found == [
            BindingConflict("shadow", ("s-up",), ("_select_up", "_scroll_up"), "picker closed")
        ]

    def test_single_key_that_prefixes_a_sequence_is_ambiguous(self):
        kb = KeyBindings()

        @kb.add("c-a")
        def _home(event):  # pragma: no cover
            pass

        @kb.add("c-a", "c-b")
        def _chord(event):  # pragma: no cover
            pass

        found = find_binding_conflicts(kb)
        assert found == [BindingConflict("prefix", ("c-a",), ("_home", "_chord"))]
        assert "timeoutlen" in found[0].describe()

    def test_eager_prefix_binding_does_not_stall(self):
        kb = KeyBindings()

        @kb.add("c-a", eager=True)
        def _home(event):  # pragma: no cover
            pass

        @kb.add("c-a", "c-b")
        def _chord(event):  # pragma: no cover
            pass

        assert find_binding_conflicts(kb) == []

    def test_prefix_with_never_active_sequence_is_ignored(self):
        kb = KeyBindings()

        @kb.add("c-a")
        def _home(event):  # pragma: no cover
            pass

        @kb.add("c-a", "c-b", filter=Condition(lambda: False))
        def _chord(event):  # pragma: no cover
            pass

        assert find_binding_conflicts(kb) == []
