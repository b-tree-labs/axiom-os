# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Headless harness and key-binding lint for prompt_toolkit TUIs.

Any extension that ships a :class:`prompt_toolkit.application.Application`
can exercise its render path, wrap math and key-binding table without a tty:

* :func:`headless_terminal` gives an already-built ``Application`` a pipe
  input and a plain-text output of a fixed, resizable size.
* :func:`find_binding_conflicts` reports key bindings that shadow each other
  in some reachable UI state, and single-key bindings that are also the head
  of a longer sequence (which makes every press wait ``timeoutlen``).
* The ``headless_terminal_factory`` fixture wraps the first for pytest and
  closes every pipe it opened at teardown.

``prompt_toolkit`` is imported lazily so that installing ``axiom-tests`` does
not require it; consumers of this module install the ``tui`` extra.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import pytest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from prompt_toolkit.application import Application
    from prompt_toolkit.input import PipeInput
    from prompt_toolkit.key_binding import KeyBindingsBase
    from prompt_toolkit.output.plain_text import PlainTextOutput

__all__ = [
    "BindingConflict",
    "HeadlessTerminal",
    "find_binding_conflicts",
    "headless_terminal",
    "headless_terminal_factory",
    "key_sequence",
]


# --------------------------------------------------------------------------
# Headless terminal
# --------------------------------------------------------------------------


@dataclass
class HeadlessTerminal:
    """A prompt_toolkit ``Application`` wired to a pipe and a sized plain-text output."""

    app: Application
    input: PipeInput
    output: PlainTextOutput
    columns: int
    rows: int
    _stack: ExitStack = field(default_factory=ExitStack, repr=False)

    def resize(self, columns: int, rows: int) -> None:
        """Change what ``app.output.get_size()`` reports from now on."""
        from prompt_toolkit.data_structures import Size

        self.columns, self.rows = columns, rows
        size = Size(rows=rows, columns=columns)
        self.output.get_size = lambda: size  # type: ignore[method-assign]

    def close(self) -> None:
        """Release the pipe behind ``app.input``. Safe to call more than once."""
        self._stack.close()


def headless_terminal(app: Application, *, columns: int = 80, rows: int = 24) -> HeadlessTerminal:
    """Attach a pipe input and a fixed-size plain-text output to *app*.

    The application is not run. The point is that everything the app derives
    from ``app.output.get_size()`` (wrap widths, borders, status lines) now has
    a deterministic answer, and key-binding handlers can be invoked directly
    with a stand-in event. Call :meth:`HeadlessTerminal.close` when done, or
    use the ``headless_terminal_factory`` fixture, which closes for you.
    """
    import io

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output.plain_text import PlainTextOutput

    stack = ExitStack()
    inp = stack.enter_context(create_pipe_input())  # a context manager since 3.0.29
    out = PlainTextOutput(io.StringIO())
    app.input = inp
    app.output = out
    term = HeadlessTerminal(
        app=app, input=inp, output=out, columns=columns, rows=rows, _stack=stack
    )
    term.resize(columns, rows)
    return term


@pytest.fixture
def headless_terminal_factory() -> Iterator[Callable[..., HeadlessTerminal]]:
    """Factory fixture: ``factory(app, columns=80, rows=24) -> HeadlessTerminal``.

    Every terminal created through the factory is closed at teardown.
    """
    opened: list[HeadlessTerminal] = []

    def _factory(app: Application, *, columns: int = 80, rows: int = 24) -> HeadlessTerminal:
        term = headless_terminal(app, columns=columns, rows=rows)
        opened.append(term)
        return term

    yield _factory
    for term in opened:
        term.close()


# --------------------------------------------------------------------------
# Key-binding lint
# --------------------------------------------------------------------------


def key_sequence(binding: Any) -> tuple[str, ...]:
    """Normalise a binding's keys to strings (``"c-c"``, ``"s-up"``, ``"a"``)."""
    return tuple(getattr(k, "value", k) for k in binding.keys)


@dataclass(frozen=True)
class BindingConflict:
    """One problem found by :func:`find_binding_conflicts`."""

    kind: Literal["shadow", "prefix"]
    keys: tuple[str, ...]
    handlers: tuple[str, ...]
    state: str = ""

    def describe(self) -> str:
        """One line a human can act on."""
        seq = "+".join(self.keys)
        if self.kind == "shadow":
            where = f" while {self.state}" if self.state else ""
            return (
                f"{seq}: {self.handlers[0]} is shadowed by {self.handlers[1]}{where} "
                f"(both filters true; last registered wins)"
            )
        return (
            f"{seq}: {self.handlers[0]} is a binding and also the prefix of "
            f"{self.handlers[1]} -> every press waits timeoutlen before resolving"
        )


def _handler_name(binding: Any) -> str:
    return getattr(binding.handler, "__name__", repr(binding.handler))


def _truthy(flt: Any) -> bool:
    """Evaluate a prompt_toolkit filter outside a running app.

    A filter that needs live app state (``has_focus`` and friends) raises here;
    it is counted as *active* so the lint stays conservative.
    """
    try:
        return bool(flt())
    except Exception:  # noqa: BLE001
        return True


def find_binding_conflicts(
    key_bindings: KeyBindingsBase,
    states: Mapping[str, Callable[[], None]] | None = None,
) -> list[BindingConflict]:
    """Return the shadowed and ambiguous bindings in *key_bindings*.

    *states* maps a readable state name to a callable that puts the UI in that
    state, e.g. ``{"picker closed": close, "picker open": open}``. Two bindings
    on one key sequence conflict only if **some** state makes both filters true
    at once; pairs the filters disambiguate are correctly ignored. With no
    *states*, filters are evaluated once, as-is. The last state entered is
    left in place; pass a final no-op state if you need a specific one.

    A single-key, non-eager binding whose key also begins a longer sequence is
    a ``prefix`` conflict: prompt_toolkit must wait ``timeoutlen`` on every
    press to see whether the longer sequence follows.
    """
    bindings = list(key_bindings.bindings)
    state_items = list((states or {"": lambda: None}).items())

    active: dict[int, dict[str, bool]] = {i: {} for i in range(len(bindings))}
    for name, enter in state_items:
        enter()
        for idx, b in enumerate(bindings):
            active[idx][name] = _truthy(b.filter)

    def ever_active(idx: int) -> bool:
        return any(active[idx].values())

    by_seq: dict[tuple[str, ...], list[int]] = {}
    for idx, b in enumerate(bindings):
        by_seq.setdefault(key_sequence(b), []).append(idx)

    conflicts: list[BindingConflict] = []

    for seq, idxs in by_seq.items():
        for i, first in enumerate(idxs):
            for second in idxs[i + 1 :]:
                for name, _ in state_items:
                    if active[first][name] and active[second][name]:
                        conflicts.append(
                            BindingConflict(
                                "shadow",
                                seq,
                                (_handler_name(bindings[first]), _handler_name(bindings[second])),
                                name,
                            )
                        )
                        break

    for seq, idxs in by_seq.items():
        if len(seq) != 1:
            continue
        for longer, lidxs in by_seq.items():
            if len(longer) < 2 or longer[0] != seq[0]:
                continue
            if not any(ever_active(l_) for l_ in lidxs):
                continue
            for short in idxs:
                if not ever_active(short) or _truthy(bindings[short].eager):
                    continue
                conflicts.append(
                    BindingConflict(
                        "prefix",
                        seq,
                        (_handler_name(bindings[short]), _handler_name(bindings[lidxs[0]])),
                    )
                )

    return conflicts
