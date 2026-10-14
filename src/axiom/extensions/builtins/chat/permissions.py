# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Per-tool permission state for the chat agent.

Three modes per tool name:

  ``allow``  — execute without prompting
  ``ask``    — fall through to the existing ApprovalGate prompt (default)
  ``deny``   — refuse without prompting

Set during a chat session by the user choosing ``A`` (Always allow) or
``D`` (Deny always) at the approval prompt, or via ``/permissions``.

Choices persist across sessions. The agent loads
``$AXI_STATE_DIR/tool_permissions.json`` (a flat ``{tool: mode}`` JSON
object) on start-up and writes it back atomically after every ``set`` or
``reset``. An unreadable or malformed file, or an entry with an unknown
mode, is ignored with a debug log so a corrupt file can never keep chat
from starting. A ``ToolPermissions`` built without a path stays purely
in-memory.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

_log = logging.getLogger(__name__)

PermissionMode = Literal["allow", "ask", "deny"]
_VALID_MODES: tuple[str, ...] = ("allow", "ask", "deny")

PERMISSIONS_FILENAME = "tool_permissions.json"

# ---------------------------------------------------------------------------
# Approval vocabulary: ONE definition for every surface
# ---------------------------------------------------------------------------
# The full-screen UI, the line REPL's render providers and the bare renderer
# present the same choices and parse the same answers. A surface may colour the
# legend; it may not add, drop or reinterpret a choice.
#
# ``h`` is the fifth outcome and the only one that does not decide. Approve,
# reject and their persisting forms all answer now, which silently assumed a
# human is at the keyboard. A surface with nobody watching had two options and
# both were bad: prompt into the void, or let an ApprovalPolicy pre-decide,
# which is a rule written in advance rather than a human in the loop. Holding
# is the third: keep the action, durably, and let a person answer from wherever
# they are.
#
# It is ``h`` and not ``d`` deliberately. ``D`` already means deny-always and is
# case-sensitive, so ``d`` would put "hold this for later" one shift key from
# "never allow this again", with no undo on the second.

ApprovalChoice = Literal["a", "A", "r", "D", "h"]

APPROVAL_LEGEND: tuple[tuple[str, str], ...] = (
    ("a", "pprove"),
    ("A", "lways allow"),
    ("r", "eject"),
    ("D", "eny always"),
    ("h", "old for a human"),
    ("s", "kip"),
)
#: Must fit the TUI's wrap width on one line. Adding ``[h]old`` pushed the old
#: wording to 75 characters and it wrapped mid-phrase, so "Choose " went rather
#: than any of the choices: a re-prompt that drops or abbreviates an outcome is
#: how a surface quietly stops offering one.
APPROVAL_RETRY = "[a]pprove, [A]lways allow, [r]eject, [D]eny always, [h]old or [s]kip"


def parse_approval_choice(raw: str) -> ApprovalChoice | None:
    """Map what the operator typed to the agent's five outcomes.

    ``A`` and ``D`` are case-sensitive because they persist across sessions.
    ``s``/``skip`` is a reject that does not persist. ``h``/``hold``/``later``
    defers to the durable queue. ``None`` means re-prompt.
    """
    text = raw.strip()
    if text == "A":
        return "A"
    if text == "D":
        return "D"
    lowered = text.lower()
    if lowered in ("a", "approve"):
        return "a"
    if lowered in ("h", "hold", "later", "defer"):
        return "h"
    if lowered in ("r", "reject", "s", "skip"):
        return "r"
    return None


def approval_legend(paint: Callable[[str], str] | None = None) -> str:
    """Render the legend; ``paint(key)`` may decorate each key, nothing else."""
    colour = paint or (lambda key: key)
    return "  ".join(f"[{colour(key)}]{rest}" for key, rest in APPROVAL_LEGEND)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict[str, PermissionMode]:
    """Read ``{tool: mode}`` from ``path``; anything unusable loads as empty."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _log.debug("ignoring unreadable tool permissions file %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        _log.debug("ignoring tool permissions file %s: expected a JSON object", path)
        return {}
    modes: dict[str, PermissionMode] = {}
    for tool, mode in raw.items():
        if isinstance(tool, str) and mode in _VALID_MODES:
            modes[tool] = mode
        else:
            _log.debug("ignoring permission %r for %r in %s", mode, tool, path)
    return modes


def _write_atomic(path: Path, modes: dict[str, PermissionMode]) -> None:
    """Replace ``path`` with ``modes`` as JSON via a same-directory temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(modes, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ToolPermissions:
    """Per-tool permission map. Default mode is ``ask``.

    With ``path`` set, the map is read from that JSON file on construction
    and written back after every ``set``/``reset``. Without a path it is
    purely in-memory.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._mode: dict[str, PermissionMode] = _load(self._path) if self._path is not None else {}

    @property
    def path(self) -> Path | None:
        """Where settings persist, or ``None`` for an in-memory map."""
        return self._path

    @staticmethod
    def default_path() -> Path:
        """``$AXI_STATE_DIR/tool_permissions.json`` (branding-aware)."""
        from axiom.infra.paths import get_user_state_dir

        return get_user_state_dir() / PERMISSIONS_FILENAME

    @classmethod
    def load_default(cls) -> ToolPermissions:
        """The user's persisted permissions; in-memory if the state dir is unusable."""
        try:
            return cls(cls.default_path())
        except OSError as exc:
            _log.warning("tool permissions will not persist this session: %s", exc)
            return cls()

    def get(self, tool: str) -> PermissionMode:
        return self._mode.get(tool, "ask")

    def set(self, tool: str, mode: PermissionMode) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"unknown permission mode {mode!r} (expected one of {_VALID_MODES})")
        self._mode[tool] = mode
        self._save()

    def reset(self, tool: str | None = None) -> None:
        """Clear one tool's setting, or all if ``tool`` is None."""
        if tool is None:
            self._mode.clear()
        else:
            self._mode.pop(tool, None)
        self._save()

    def all(self) -> dict[str, PermissionMode]:
        """Return only tools with explicit settings."""
        return dict(self._mode)

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            _write_atomic(self._path, self._mode)
        except OSError as exc:
            # The in-memory choice still applies for this session.
            _log.warning("could not persist tool permissions to %s: %s", self._path, exc)


def format_permissions(perms: ToolPermissions) -> str:
    """Render the permission table for ``/permissions``."""
    items = perms.all()
    if not items:
        lines = [
            "",
            "  No tool overrides set.",
            "  All tools default to: ask (write tools prompt; read tools auto-approve).",
        ]
    else:
        # `{name:30s}` truncates nothing but pads a long tool name past the
        # mode beside it; the table sizes the column to what is actually
        # there. No border — this renders inside the prompt-toolkit TUI.
        from axiom.infra.cli_format import Column, table

        lines = ["", "  Tool permissions:"]
        lines.extend(
            table(
                sorted(items.items()),
                [Column("tool", wrap=True), Column("mode")],
                width=76,
                indent=2,
            )
        )
    if perms.path is not None:
        lines.append(f"  Stored in: {perms.path}")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "APPROVAL_LEGEND",
    "APPROVAL_RETRY",
    "PERMISSIONS_FILENAME",
    "ApprovalChoice",
    "PermissionMode",
    "ToolPermissions",
    "approval_legend",
    "format_permissions",
    "parse_approval_choice",
]
