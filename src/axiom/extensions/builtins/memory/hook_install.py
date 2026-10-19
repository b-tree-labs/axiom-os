# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Register / unregister at-turn capture in the harness's settings.

Claude Code reads hooks from ``~/.claude/settings.json``. Installing
capture means adding a ``Stop`` hook (fires when an assistant response
completes) and a ``SessionEnd`` hook (fires when a session closes) that
invoke ``axi memory hook``.

This edits a file the user owns and did not necessarily ask us to
rewrite, so the rules here are conservative:

- **Back up before writing.** Always, to a timestamp-free sibling path
  the user can find.
- **Refuse to write what we could not read.** If the settings file does
  not parse, stop. Overwriting it would destroy configuration we cannot
  see, which is a far worse outcome than not installing a hook.
- **Never clobber somebody else's hooks.** Ours are identified by a
  marker in the command string; everything else in the file is
  preserved exactly, including other ``Stop`` hooks.

Both events invoke the same idempotent command, so a turn captured at
``Stop`` is a no-op at ``SessionEnd``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

#: Substring identifying a hook entry as ours, for idempotent install
#: and surgical uninstall.
HOOK_MARKER = "axi-memory-capture"

#: Events we register on. Stop is per-turn; SessionEnd catches whatever
#: a turn-level hook missed (a skipped lock, a final turn, a crash).
HOOK_EVENTS = ("Stop", "SessionEnd")


def _default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _hook_command(axi_path: str) -> str:
    """The shell command Claude Code runs for each event.

    Drains stdin first, then hands the payload to a detached ingest so
    the user's turn is never waiting on a ledger write. The trailing
    marker comment is how install/uninstall recognise our entry.
    """
    return (
        f'payload=$(cat); tmp=$(mktemp); printf "%s" "$payload" > "$tmp"; '
        f'nohup {axi_path} memory hook --payload-file "$tmp" --unlink-payload '
        f'>/dev/null 2>&1 & exit 0  # {HOOK_MARKER}'
    )


def _load_settings(settings_path: Path) -> dict[str, Any]:
    if not settings_path.exists():
        return {}
    raw = settings_path.read_text()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{settings_path} is not valid JSON ({exc}); refusing to "
            "rewrite it. Fix or move the file, then re-run."
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"{settings_path} does not contain a JSON object")
    return data


def _is_ours(entry: dict[str, Any]) -> bool:
    return any(
        HOOK_MARKER in (hook.get("command") or "")
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict)
    )


def install_capture_hook(
    *,
    settings_path: Path | str | None = None,
    axi_path: str | None = None,
) -> dict[str, Any]:
    """Add the capture hook to the harness settings. Idempotent."""
    path = Path(settings_path) if settings_path else _default_settings_path()
    resolved_axi = axi_path or (Path.home() / ".axi" / "service-venv" / "bin" / "axi")
    data = _load_settings(path)

    backup = path.with_suffix(path.suffix + ".axi-hook-backup")
    if path.exists():
        shutil.copyfile(path, backup)

    hooks = data.setdefault("hooks", {})
    command = _hook_command(str(resolved_axi))
    installed: list[str] = []

    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        # Drop any previous version of ours, keep everyone else's.
        kept = [e for e in entries if not (isinstance(e, dict) and _is_ours(e))]
        kept.append({"hooks": [{"type": "command", "command": command}]})
        hooks[event] = kept
        installed.append(event)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")

    return {
        "settings_path": str(path),
        "backup": str(backup),
        "events": installed,
        "axi": str(resolved_axi),
    }


def uninstall_capture_hook(
    *, settings_path: Path | str | None = None,
) -> dict[str, Any]:
    """Remove only our hook entries, leaving every other hook intact."""
    path = Path(settings_path) if settings_path else _default_settings_path()
    data = _load_settings(path)

    backup = path.with_suffix(path.suffix + ".axi-hook-backup")
    if path.exists():
        shutil.copyfile(path, backup)

    hooks = data.get("hooks") or {}
    removed = 0
    for event in list(hooks):
        entries = hooks.get(event) or []
        kept = [e for e in entries if not (isinstance(e, dict) and _is_ours(e))]
        removed += len(entries) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)

    path.write_text(json.dumps(data, indent=2) + "\n")
    return {"settings_path": str(path), "removed": removed, "backup": str(backup)}
