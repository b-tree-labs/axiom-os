# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Provider ABCs for chat UI — render and input contracts.

These abstract base classes define the contracts that the chat engine
works through. Concrete implementations (ANSI/Rich, basic/PTK) are
selected at runtime based on available dependencies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axiom.infra.gateway import StreamChunk
    from axiom.infra.orchestrator.actions import Action


class RenderProvider(ABC):
    """Renders chat output to the terminal."""

    @abstractmethod
    def stream_text(self, chunks: Iterator[StreamChunk]) -> str:
        """Stream LLM text deltas to the terminal, return accumulated text."""
        ...

    @abstractmethod
    def render_welcome(
        self,
        gateway: Any = None,
        show_banner: bool = False,
        workspace_context: str = "",
    ) -> None:
        """Print the chat welcome banner with gateway status.

        Args:
            gateway: LLM gateway for status display.
            show_banner: If True, show the full salamander mascot banner
                (used when chat is entered directly rather than as a
                subcommand).
            workspace_context: If non-empty, display workspace info in the banner.
        """
        ...

    @abstractmethod
    def render_tool_start(self, name: str, params: dict[str, Any]) -> None:
        """Show that a tool is starting execution (spinner or label)."""
        ...

    @abstractmethod
    def render_tool_result(self, name: str, result: dict[str, Any], elapsed: float) -> None:
        """Show a compact tool result with elapsed time."""
        ...

    @abstractmethod
    def render_approval_prompt(self, action: Action) -> str:
        """Render an approval prompt and return user choice.

        Returns: "a" for approve, "A" for always, "r" for reject.
        """
        ...

    @abstractmethod
    def render_action_result(self, action: Action) -> None:
        """Print the result of a completed/rejected/failed action."""
        ...

    @abstractmethod
    def render_status(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost: float,
        tier: str | None = None,
    ) -> None:
        """Show a status line after each turn (model, tier, tokens, cost).

        ``tier`` is the routing tier the turn was answered at. It was already
        accepted by one provider and declared by none, so no caller could rely
        on it; it is part of the contract now because the tier is the knowledge
        dial, and which one answered is the most load-bearing fact about an
        answer after the answer itself.

        A turn with no model and no tokens renders nothing, rather than a
        status line reading "0 in, 0 out".
        """
        ...

    @abstractmethod
    def render_thinking(self, text: str, collapsed: bool = True) -> None:
        """Display a thinking/reasoning block from the LLM."""
        ...

    @abstractmethod
    def render_message(self, role: str, content: str) -> None:
        """Print a chat message with role prefix and formatting."""
        ...

    @abstractmethod
    def render_session_list(self, sessions: list[dict[str, Any]]) -> None:
        """Render a formatted list of saved sessions."""
        ...

    @abstractmethod
    def render_citations(self, chunks: Sequence[Any]) -> None:
        """Show what grounded the answer just rendered.

        ``chunks`` are the ``RetrievedChunk``s the turn retrieved, in rank
        order. A surface shows one entry per SOURCE, not per chunk: three
        chunks of one document are one citation.

        Abstract rather than defaulted to a no-op on purpose. A defaulted
        no-op is how the web surface came to silently drop the model, the
        token counts, the cost and the reasoning while the terminal rendered
        all four — a provider that does not want to show citations says so in
        its own body, where a reader can see the decision.

        An empty sequence renders nothing at all, rather than an empty
        "Sources" heading.
        """
        ...


class InputProvider(ABC):
    """Handles user input with optional history and autocomplete."""

    _MODES = ("Ask", "Plan", "Agent")

    @property
    def mode(self) -> str:
        """Current interaction mode: Ask, Plan, or Agent."""
        return getattr(self, "_mode", "Ask")

    @mode.setter
    def mode(self, value: str) -> None:
        if value not in self._MODES:
            raise ValueError(f"Unknown mode: {value}. Choose from {self._MODES}")
        self._mode = value

    def cycle_mode(self) -> str:
        """Advance to the next mode and return its name."""
        idx = self._MODES.index(self.mode)
        self._mode = self._MODES[(idx + 1) % len(self._MODES)]
        return self._mode

    @abstractmethod
    def prompt(self, prefix: str = "you> ", show_border: bool = False) -> str:
        """Read a line of user input with the given prefix.

        Args:
            prefix: The prompt text shown before the cursor.
            show_border: If True, show a bottom border below the input line
                (rendered live while typing if the provider supports it).

        Raises:
            EOFError: On Ctrl+D
            KeyboardInterrupt: On Ctrl+C
        """
        ...

    @abstractmethod
    def prompt_choice(self, options: list[str]) -> str:
        """Prompt the user to choose from a list of options.

        Returns the chosen option string.
        """
        ...

    def setup(self, slash_commands: list[str] | None = None) -> None:
        """Initialize the input provider (history file, completers, etc.)."""
        pass

    def teardown(self) -> None:
        """Clean up resources (save history, etc.)."""
        pass


def citation_entries(
    chunks: Sequence[Any],
    *,
    url_for: Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    """The renderable form of a turn's retrieval: one entry per SOURCE.

    ``url_for`` maps a source path to a link that opens the document, or None
    when the deployment cannot serve it. It is a caller-supplied resolver
    because the platform does not know how a given node serves documents (a
    signed URL, a gateway path, a Library deep link); it does know that a
    citation without one is a name, not provenance. When no resolver is given
    the entry carries ``"url": None`` so a surface can say "not yet available"
    rather than render a dead link.

    Several chunks of one document are one citation. The best (lowest) rank
    wins, so a document that matched strongly once and weakly twice is ordered
    by its strong match, which is the order a reader expects.

    Shared by every provider so the terminal and the browser cannot disagree
    about what the sources of an answer were.
    """
    best: dict[str, Any] = {}
    for c in chunks:
        path = getattr(c, "source_path", "") or ""
        prev = best.get(path)
        if prev is None or getattr(c, "rank", 0) < getattr(prev, "rank", 0):
            best[path] = c
    return [
        {
            "key": getattr(c, "citation_key", ""),
            "rank": getattr(c, "rank", 0),
            "title": getattr(c, "source_title", "") or getattr(c, "source_path", ""),
            "path": getattr(c, "source_path", ""),
            "corpus": getattr(c, "corpus", ""),
            "url": _url_or_none(url_for, getattr(c, "source_path", "")),
        }
        for c in sorted(best.values(), key=lambda c: getattr(c, "rank", 0))
    ]


def _url_or_none(url_for: Callable[[str], str | None] | None, path: str) -> str | None:
    """A resolver that raises must not take the citation with it; the answer
    is still grounded, it just cannot be opened from here."""
    if url_for is None or not path:
        return None
    try:
        return url_for(path) or None
    except Exception:  # noqa: BLE001 — a link is optional, provenance is not
        return None
