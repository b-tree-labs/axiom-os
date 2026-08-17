# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rich render provider — enhanced terminal output using rich library.

Uses rich.Console, rich.Live, rich.Markdown, and rich.Syntax for
high-quality terminal rendering with syntax highlighting, tables,
diffs, and streaming markdown.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from .base import RenderProvider

if TYPE_CHECKING:
    from axiom.infra.gateway import StreamChunk
    from axiom.infra.orchestrator.actions import Action

try:
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.text import Text
    from rich.theme import Theme

    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False
    Console = None  # type: ignore[assignment,misc]
    Live = None  # type: ignore[assignment,misc]
    Markdown = None  # type: ignore[assignment,misc]
    Panel = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    Theme = None  # type: ignore[assignment,misc]

_DIFF_RE = re.compile(r"^(---|\+\+\+|@@|[-+])\s", re.MULTILINE)


def _is_diff(text: str) -> bool:
    """Detect if text looks like unified diff format."""
    lines = text.splitlines()
    if len(lines) < 3:
        return False
    diff_markers = sum(1 for line in lines if line.startswith(("+", "-", "@@", "---", "+++")))
    return diff_markers / max(len(lines), 1) > 0.3


def _max_width() -> int:
    """Terminal width capped at 120 for readability."""
    try:
        import shutil

        return min(shutil.get_terminal_size().columns, 120)
    except Exception:
        return 80


class RichRenderProvider(RenderProvider):
    """Rich-powered render provider with syntax highlighting and streaming."""

    def __init__(self):
        if not _RICH_AVAILABLE:
            raise ImportError("rich is required for RichRenderProvider")

        assert Theme is not None
        assert Console is not None
        custom_theme = Theme(
            {
                "thinking": "dim italic",
                "tool.name": "bold cyan",
                "tool.ok": "green",
                "tool.fail": "red",
                "status": "dim",
            }
        )
        self.console = Console(
            width=_max_width(),
            theme=custom_theme,
            highlight=False,
        )
        self._active_spinner = None

    def stream_text(self, chunks: Iterator[StreamChunk]) -> str:
        assert Markdown is not None and Live is not None  # guarded by __init__
        import sys

        from ..pulse_spinner import PulseSpinner

        accumulated = ""
        is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

        # Phase 1: Show pulse spinner BEFORE Rich Live context.
        # Rich's Live intercepts stdout, so the spinner must run outside it.
        spinner: PulseSpinner | None = None
        if is_tty:
            spinner = PulseSpinner("Thinking")
            spinner.start()

        # Consume chunks until first renderable content arrives
        got_done = False
        for chunk in chunks:
            if chunk.type == "text":
                if spinner:
                    spinner.stop()
                    spinner = None
                accumulated += chunk.text
                break
            elif chunk.type == "thinking_delta":
                if spinner:
                    spinner.set_sub_state("reasoning")
            elif chunk.type == "usage":
                if spinner:
                    spinner.update_tokens(
                        input_tokens=chunk.input_tokens,
                        output_tokens=chunk.output_tokens,
                    )
            elif chunk.type in ("tool_use_start", "done"):
                if spinner:
                    spinner.stop()
                    spinner = None
                if chunk.type == "done":
                    got_done = True
                break

        # Phase 2: Stream remaining chunks inside Rich Live (spinner is stopped)
        if got_done or not accumulated:
            # Nothing to stream — either done or no text yet
            if accumulated:
                self.console.print(Markdown(accumulated))
            return accumulated

        try:
            with Live(
                Markdown(accumulated),
                console=self.console,
                refresh_per_second=15,
                vertical_overflow="visible",
            ) as live:
                for chunk in chunks:
                    if chunk.type == "text":
                        accumulated += chunk.text
                        live.update(Markdown(accumulated))

                    elif chunk.type == "tool_use_start":
                        live.update(Markdown(accumulated))

                    elif chunk.type == "done":
                        live.update(Markdown(accumulated))
                        break
        except Exception:
            if accumulated:
                self.console.print(Markdown(accumulated))

        return accumulated

    def render_welcome(
        self,
        gateway=None,
        show_banner: bool = False,
        workspace_context: str = "",
    ) -> None:
        assert Text is not None  # guarded by __init__
        self.console.print()
        if show_banner:
            from axiom.setup.renderer import _BANNER

            banner_text = Text(_BANNER.strip("\n"), style="bold #00cfff")
            self.console.print(banner_text)
        else:
            title = Text("axi chat", style="bold #00cfff")
            self.console.print("  ", title)

        if gateway is not None:
            provider = gateway.active_provider
            if provider:
                model_name = getattr(gateway, "_model_override", None) or provider.model
                status = Text(f"  {provider.name} ", style="dim")
                status.append(f"({model_name})", style="green")
            else:
                status = Text("  No LLM configured. ", style="yellow")
                status.append("Run `axi config` to set up.", style="dim")
            self.console.print(status)

        # Workspace context from model.yaml
        if workspace_context:
            from ..workspace import workspace_summary_line

            summary = workspace_summary_line(workspace_context)
            if summary:
                ws_text = Text(f"  {summary}", style="dim green")
                self.console.print(ws_text)
            # Show materials line if present
            for line in workspace_context.splitlines()[1:]:
                if line.startswith("Materials:"):
                    mat_text = Text(f"    {line}", style="dim")
                    self.console.print(mat_text)
                    break

        tips = Text("  ", style="dim")
        tips.append("/help", style="dim cyan")
        tips.append(" for commands  ", style="dim")
        tips.append("ctrl-c", style="dim cyan")
        tips.append(" to interrupt  ", style="dim")
        tips.append("/exit", style="dim cyan")
        tips.append(" to quit", style="dim")
        self.console.print(tips)
        self.console.print()

    def render_tool_start(self, name: str, params: dict[str, Any]) -> None:
        key_params = ""
        if params:
            items = list(params.items())[:2]
            key_params = " " + " ".join(f"{k}={v}" for k, v in items)
        self.console.print(f"  [tool.name]{name}[/]{key_params} ...", end="")
        self._active_spinner = True

    def render_tool_result(self, name: str, result: dict[str, Any], elapsed: float) -> None:
        # Clear previous line
        if self._active_spinner:
            self.console.print("\r" + " " * 60, end="\r")
            self._active_spinner = None

        if "error" in result:
            self.console.print(
                f"  [tool.fail]x[/] {name} failed ({elapsed:.1f}s): {result['error']}"
            )
        else:
            self.console.print(f"  [tool.ok]v[/] {name} ({elapsed:.1f}s)")

    def render_approval_prompt(self, action: Action) -> str:
        self.console.print()
        self.console.print("  [yellow]--- Write operation ---[/]")
        params = _format_params(action.params)
        self.console.print(f"  [bold]{action.name}[/]: {params}")
        self.console.print("  [yellow]" + "-" * 30 + "[/]")
        self.console.print(
            "  [green]\\[a][/]pprove  "
            "[green]\\[A][/]lways allow  "
            "[red]\\[r][/]eject  "
            "[yellow]\\[s][/]kip"
        )
        self.console.print()

        while True:
            try:
                choice = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                return "r"
            if choice in ("a", "approve"):
                return "a"
            elif choice == "A":
                return "A"
            elif choice in ("r", "reject", "s", "skip"):
                return "r"
            else:
                self.console.print("  Choose [a]pprove, [A]lways allow, [r]eject, or [s]kip")

    def render_action_result(self, action: Action) -> None:
        from axiom.infra.orchestrator.actions import ActionStatus

        if action.status == ActionStatus.COMPLETED:
            result = action.result or {}
            if "error" in result:
                self.console.print(f"  [red]![/] {result['error']}")
            elif "url" in result:
                self.console.print(
                    f"  [green]Published:[/] {result['url']} ({result.get('version', '')})"
                )
            elif "output" in result:
                self.console.print(f"  [green]Generated:[/] {result['output']}")
            elif "documents" in result:
                docs = result["documents"]
                if not docs:
                    self.console.print("  No tracked documents.")
                else:
                    for d in docs:
                        self.console.print(
                            f"  {d['doc_id']}: {d['status']} ({d.get('version', '')})"
                        )
            elif "changed" in result:
                changed = result["changed"]
                if not changed:
                    self.console.print("  No changes since last publish.")
                else:
                    for c in changed:
                        self.console.print(f"  {c}")
            else:
                for k, v in result.items():
                    self.console.print(f"  {k}: {v}")
        elif action.status == ActionStatus.REJECTED:
            self.console.print(f"  [yellow]\\[skipped][/] {action.name}")
        elif action.status == ActionStatus.FAILED:
            self.console.print(f"  [red]\\[failed][/] {action.error}")

    def render_status(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost: float,
    ) -> None:
        parts = []
        if model:
            parts.append(model)
        parts.append(f"{tokens_in}in/{tokens_out}out")
        if cost > 0:
            parts.append(f"${cost:.4f}")
        line = " | ".join(parts)
        self.console.print(f"  [status]{line}[/]")

    def render_thinking(self, text: str, collapsed: bool = True) -> None:
        assert Panel is not None and Text is not None  # guarded by __init__
        if not text:
            return
        lines = text.splitlines()
        if collapsed and len(lines) > 2:
            display_text = "\n".join(lines[:2]) + f"\n... ({len(lines) - 2} more lines)"
        else:
            display_text = text

        panel = Panel(
            Text(display_text, style="thinking"),
            title="Thinking",
            border_style="dim",
            expand=False,
            padding=(0, 1),
        )
        self.console.print(panel)

    def render_message(self, role: str, content: str) -> None:
        assert Markdown is not None  # guarded by __init__
        if role == "assistant":
            self.console.print()
            self.console.print(Markdown(content))
            self.console.print()
        elif role == "user":
            pass
        elif role == "system":
            self.console.print(f"  [dim]\\[system] {content}[/]")
        elif role == "tool_result":
            self.console.print(f"  [dim]{content}[/]")

    def render_session_list(self, sessions: list[dict[str, Any]]) -> None:
        if not sessions:
            self.console.print("  No saved sessions.")
            return

        self.console.print()
        self.console.print("  [bold]Saved sessions:[/]")
        for s in sessions:
            sid = s.get("id", "?")
            msgs = s.get("messages", 0)
            updated = s.get("updated", "")
            self.console.print(f"  [cyan]{sid}[/]  {msgs} messages  [dim]{updated}[/]")
        self.console.print()


def _format_params(params: dict[str, Any]) -> str:
    if not params:
        return "(no parameters)"
    parts = []
    for k, v in params.items():
        parts.append(f"{k}={v}")
    return "  |  ".join(parts)
