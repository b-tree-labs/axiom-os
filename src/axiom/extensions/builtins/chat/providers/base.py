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
from collections.abc import Iterator
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
                (used when entering via bare ``neut`` command).
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
    ) -> None:
        """Show a status line after each turn (model, tokens, cost)."""
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
