# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Prompt Toolkit input provider — history, autocomplete, keybindings.

Uses prompt_toolkit.PromptSession for a rich input experience with:
- FileHistory saved to ~/.config/neut/chat_history
- Slash command autocomplete with descriptions
- @file path completion
- !bash escape
- Shift+Tab mode cycling (Ask / Plan / Agent)
- Native multiline input (Alt+Enter or Ctrl+J for newline, Enter to send)
- Trailing backslash multiline continuation
- Tab-queue: Tab on a /command adds to queue, drained after each turn
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from axiom.infra.branding import get_branding as _get_branding

from .base import InputProvider

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion, PathCompleter, WordCompleter
    from prompt_toolkit.document import Document
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style

    _PTK_AVAILABLE = True
except ImportError:
    _PTK_AVAILABLE = False
    PromptSession = None  # type: ignore[assignment,misc]
    WordCompleter = None  # type: ignore[assignment,misc]
    Completer = object  # type: ignore[assignment,misc]
    Completion = None  # type: ignore[assignment,misc]
    PathCompleter = None  # type: ignore[assignment,misc]
    Document = None  # type: ignore[assignment,misc]
    FileHistory = None  # type: ignore[assignment,misc]
    HTML = None  # type: ignore[assignment,misc]
    KeyBindings = None  # type: ignore[assignment,misc]
    Style = None  # type: ignore[assignment,misc]


_HISTORY_DIR = Path.home() / ".config" / _get_branding().cli_name
_HISTORY_FILE = _HISTORY_DIR / "chat_history"

# Container bg:default + noreverse ensures our inline styles aren't overridden
assert Style is not None  # always available at module level when ptk is installed
_PTK_STYLE = Style.from_dict(
    {
        "prompt": "#00cfff bold",
        "continuation": "ansibrightblack",
        "bottom-toolbar": "bg:default noreverse",
        "bottom-toolbar.text": "bg:default noreverse",
    }
)


class ChatCompleter(Completer):
    """Completer for the chat REPL.

    - Typing `/` shows the slash-command palette (filtered, with descriptions).
    - Typing `/model ` or `/resume ` switches to arg-mode (providers/sessions).
    - Typing `@` delegates to PathCompleter for file paths.
    - Multi-word commands like `/sessions rename` are excluded from the palette.
    """

    def __init__(
        self,
        slash_commands: dict[str, str],
        providers_fn: Callable[[], list[str]] | None = None,
        sessions_fn: Callable[[], list[str]] | None = None,
    ) -> None:
        self._slash_commands = slash_commands
        self._providers_fn = providers_fn
        self._sessions_fn = sessions_fn
        # Only single-word commands go in the palette
        self._palette = {
            cmd: desc
            for cmd, desc in slash_commands.items()
            if len(cmd.split()) == 1
        }
        if _PTK_AVAILABLE:
            self._path_completer = PathCompleter(expanduser=True)

    def get_completions(self, document: Document, complete_event):
        if not _PTK_AVAILABLE:
            return

        text = document.text_before_cursor

        # @file completion
        tokens = text.split()
        last_token = tokens[-1] if tokens else ""
        if last_token.startswith("@"):
            path_prefix = last_token[1:]
            # Delegate to PathCompleter using a sub-document
            sub_doc = Document(path_prefix, cursor_position=len(path_prefix))
            for c in self._path_completer.get_completions(sub_doc, complete_event):
                yield Completion(c.text, c.start_position, display_meta=c.display_meta)
            return

        if not text.startswith("/"):
            return

        # Arg-mode: /model <providers> or /resume <sessions>
        parts = text.split(" ", 1)
        head = parts[0].lower()
        if len(parts) == 2:
            arg_prefix = parts[1]
            if head == "/model" and self._providers_fn:
                for name in self._providers_fn():
                    if name.startswith(arg_prefix):
                        yield Completion(name[len(arg_prefix):], display=name)
            elif head == "/resume" and self._sessions_fn:
                for sid in self._sessions_fn():
                    if sid.startswith(arg_prefix):
                        yield Completion(sid[len(arg_prefix):], display=sid)
            return

        # Palette mode: filter single-word slash commands
        prefix = text.lower()
        for cmd, desc in self._palette.items():
            if cmd.lower().startswith(prefix):
                yield Completion(
                    cmd[len(text):],
                    start_position=0,
                    display=cmd,
                    display_meta=desc,
                )


class PTKInputProvider(InputProvider):
    """prompt_toolkit-powered input with history and autocomplete."""

    def __init__(self):
        if not _PTK_AVAILABLE:
            raise ImportError("prompt_toolkit is required for PTKInputProvider")

        self._session: PromptSession | None = None  # type: ignore[valid-type]
        self._completer: ChatCompleter | None = None
        self._mode: str = "Ask"
        self._queued: list[str] = []

    def enqueue(self, cmd: str) -> None:
        """Add a slash command to the post-turn queue."""
        self._queued.append(cmd)

    def drain_queue(self) -> list[str]:
        """Return and clear the queued commands."""
        cmds = list(self._queued)
        self._queued.clear()
        return cmds

    def _build_toolbar(self):
        """Build 2-line bottom toolbar: thin rule + mode indicator with spacing."""
        import shutil

        try:
            width = shutil.get_terminal_size().columns
        except Exception:
            width = 80
        mode_label = self._mode.lower()
        rule = "─" * width
        return [
            ("#555555 bg:default noreverse", rule),
            ("bg:default noreverse", "\n"),
            ("#aaaaaa bold bg:default noreverse", " ⏵⏵ "),
            ("#aaaaaa bold bg:default noreverse", f"{mode_label} mode"),
            ("#666666 bg:default noreverse", "  (shift+tab to cycle)"),
            ("#555555 bg:default noreverse", "  ·  "),
            ("#666666 bg:default noreverse", "alt+enter for newline"),
        ]

    def setup(
        self,
        slash_commands: dict[str, str] | list[str] | None = None,
        providers_fn: Callable[[], list[str]] | None = None,
        sessions_fn: Callable[[], list[str]] | None = None,
    ) -> None:
        assert KeyBindings is not None
        assert PromptSession is not None
        assert FileHistory is not None

        # Normalize slash_commands to dict
        if slash_commands is None:
            cmds_dict: dict[str, str] = {
                "/help": "Show commands",
                "/status": "Session info",
                "/sessions": "Browse sessions",
                "/resume": "Load session",
                "/new": "New session",
                "/exit": "Exit",
            }
        elif isinstance(slash_commands, list):
            cmds_dict = {cmd: "" for cmd in slash_commands}
        else:
            cmds_dict = dict(slash_commands)

        # Ensure history directory exists
        _HISTORY_DIR.mkdir(parents=True, exist_ok=True)

        self._completer = ChatCompleter(
            slash_commands=cmds_dict,
            providers_fn=providers_fn,
            sessions_fn=sessions_fn,
        )

        # Keybindings
        kb = KeyBindings()

        @kb.add("s-tab")
        def _cycle_mode(event):
            self.cycle_mode()
            event.app.invalidate()  # Force toolbar redraw

        # Multiline: Enter submits, Alt+Enter or Ctrl+J inserts newline
        @kb.add("enter")
        def _submit(event):
            buf = event.current_buffer
            text = buf.text
            # Trailing backslash → continue editing
            if text.rstrip().endswith("\\"):
                buf.delete_before_cursor(count=1)  # strip the backslash
                buf.insert_text("\n")
                return
            buf.validate_and_handle()

        @kb.add("escape", "enter")
        def _newline_alt(event):
            event.current_buffer.insert_text("\n")

        @kb.add("c-j")
        def _newline_ctrl_j(event):
            event.current_buffer.insert_text("\n")

        @kb.add("tab")
        def _tab_queue(event):
            buf = event.current_buffer
            text = buf.text
            # Tab on a /command (with content) and no active completion → queue it
            if (
                text.startswith("/")
                and len(text) > 1
                and buf.complete_state is None
            ):
                self._queued.append(text)
                buf.reset()
            else:
                buf.start_completion(select_first=False)

        self._session = PromptSession(
            history=FileHistory(str(_HISTORY_FILE)),
            completer=self._completer,
            style=_PTK_STYLE,
            key_bindings=kb,
            enable_history_search=True,
            multiline=True,
            enable_open_in_editor=True,
        )

    def teardown(self) -> None:
        self._session = None

    def prompt(self, prefix: str = "you> ", show_border: bool = False) -> str:
        assert HTML is not None  # guarded by __init__
        if self._session is None:
            self.setup()

        # Use HTML formatting for the prompt prefix
        if prefix in ("you> ", "> "):
            formatted_prefix = HTML("<prompt>&gt; </prompt>")
        else:
            formatted_prefix = prefix

        # Continuation lines show dim "...> " prefix
        _HTML = HTML  # local binding for pyright narrowing

        def _continuation(width, _line_number, wrap_count):
            if wrap_count:
                return _HTML("<continuation>     </continuation>")
            return _HTML("<continuation>...&gt; </continuation>")

        # Show bottom toolbar with mode indicator while typing
        toolbar = self._build_toolbar if show_border else None

        assert self._session is not None, "call setup() before prompt()"
        result = self._session.prompt(
            formatted_prefix,
            bottom_toolbar=toolbar,
            prompt_continuation=_continuation,
        )
        return result

    def prompt_choice(self, options: list[str]) -> str:
        """Prompt with arrow-key selection if available, fallback to numbered list."""
        try:
            from prompt_toolkit.shortcuts import radiolist_dialog

            result = radiolist_dialog(
                title="Select an option",
                values=[(opt, opt) for opt in options],
                style=_PTK_STYLE,
            ).run()
            return result if result else (options[0] if options else "")
        except Exception:
            # Fallback to numbered list
            for i, opt in enumerate(options, 1):
                print(f"  {i}. {opt}")
            while True:
                try:
                    raw = input("  > ").strip()
                except (EOFError, KeyboardInterrupt):
                    return options[0] if options else ""
                if raw.isdigit():
                    idx = int(raw) - 1
                    if 0 <= idx < len(options):
                        return options[idx]
                for opt in options:
                    if raw.lower() == opt.lower():
                        return opt
                print(f"  Please enter a number from 1 to {len(options)}")

    def prompt_session_picker(self, sessions: list[dict]) -> str | None:
        """Interactive session picker with arrow keys. Returns session ID or None."""
        if not sessions:
            return None
        try:
            from prompt_toolkit.shortcuts import radiolist_dialog

            values = []
            for s in sessions:
                sid = s["id"]
                title = s.get("title") or "(untitled)"
                msgs = s.get("message_count", 0)
                label = f"{sid}  {title}  ({msgs} msgs)"
                values.append((sid, label))

            result = radiolist_dialog(
                title="Select a session (arrow keys + Enter)",
                values=values,
                style=_PTK_STYLE,
            ).run()
            return result
        except Exception:
            return None
