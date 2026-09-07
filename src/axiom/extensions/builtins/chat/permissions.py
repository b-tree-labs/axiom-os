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
# present the same five choices and parse the same answers. A surface may
# colour the legend; it may not add, drop or reinterpret a choice. The agent
# loop consumes the four outcomes ("s" is a non-persisting reject).

ApprovalChoice = Literal["a", "A", "r", "D"]

APPROVAL_LEGEND: tuple[tuple[str, str], ...] = (
    ("a", "pprove"),
    ("A", "lways allow"),
    ("r", "eject"),
    ("D", "eny always"),
    ("s", "kip"),
)
APPROVAL_RETRY = "Choose [a]pprove, [A]lways allow, [r]eject, [D]eny always, or [s]kip"


def parse_approval_choice(raw: str) -> ApprovalChoice | None:
    """Map what the operator typed to the agent's four outcomes.

    ``A`` and ``D`` are case-sensitive because they persist across sessions.
    ``s``/``skip`` is a reject that does not persist. ``None`` means re-prompt.
    """
    text = raw.strip()
    if text == "A":
        return "A"
    if text == "D":
        return "D"
    lowered = text.lower()
    if lowered in ("a", "approve"):
        return "a"
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
        lines = ["", "  Tool permissions:"]
        for name, mode in sorted(items.items()):
            lines.append(f"    {name:30s}  {mode}")
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
