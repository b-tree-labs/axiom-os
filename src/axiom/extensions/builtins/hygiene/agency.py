# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TIDY's bounded agency: kill switch, blast-radius caps, rate limits, Needs you.

Default is to *act* inside policy (RIVET-shaped safe agency), not to only
report. A dry-run flag and a kill switch remain. Caps and a hard never-touch
list bound every run. Tier 2 asks go to a person's Needs you (HERALD inbox
plus an approval-gate write).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

_log = logging.getLogger("axiom.hygiene.agency")

# Kill switch: setting or env. Env wins when set.
ENABLED_SETTING = "hygiene.storage_enabled"
WATCH_ONLY_SETTING = "hygiene.storage_watch_only"
ENABLED_ENV = "AXIOM_HYGIENE_STORAGE_ENABLED"
WATCH_ONLY_ENV = "AXIOM_HYGIENE_STORAGE_WATCH_ONLY"
MAX_ITEMS_SETTING = "hygiene.storage_max_items_per_run"
MAX_GIB_SETTING = "hygiene.storage_max_gib_per_run"
MAX_RUNS_SETTING = "hygiene.storage_max_runs_per_hour"

DEFAULT_MAX_ITEMS = 200
DEFAULT_MAX_GIB = 2.0
AGGRESSIVE_MAX_ITEMS = 500
AGGRESSIVE_MAX_GIB = 4.0
DEFAULT_MAX_RUNS_PER_HOUR = 4

RECOVERY_ARTIFACT = "Re-run the source CI workflow to regenerate this artifact."
RECOVERY_CACHE = "The next CI run on this ref will rebuild the cache."

_FALSEY = frozenset({"0", "false", "off", "no", "disabled"})


class SizedItem(Protocol):
    size_bytes: int


@dataclass(frozen=True)
class BlastCaps:
    max_items: int = DEFAULT_MAX_ITEMS
    max_gib: float = DEFAULT_MAX_GIB

    @property
    def max_bytes(self) -> int:
        return int(self.max_gib * (1024**3))


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in _FALSEY and str(value).strip() != ""


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw).strip().lower() not in _FALSEY


def _settings(get: Callable[[str, Any], Any] | None, key: str, default: Any) -> Any:
    if get is not None:
        return get(key, default)
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        return SettingsStore().get(key, default)
    except Exception:  # noqa: BLE001
        return default


def storage_enabled(settings_get: Callable[[str, Any], Any] | None = None) -> bool:
    """False when the kill switch is set. Env overrides the setting."""
    env = _env_bool(ENABLED_ENV)
    if env is not None:
        return env
    return _as_bool(_settings(settings_get, ENABLED_SETTING, True), True)


def storage_watch_only(settings_get: Callable[[str, Any], Any] | None = None) -> bool:
    """True only when an operator explicitly wants dry-run as the default."""
    env = _env_bool(WATCH_ONLY_ENV)
    if env is not None:
        return env
    return _as_bool(_settings(settings_get, WATCH_ONLY_SETTING, False), False)


def decide_act(
    params: dict[str, Any],
    *,
    settings_get: Callable[[str, Any], Any] | None = None,
    scope: str = "storage",
) -> tuple[bool, str]:
    """Return (act, reason). Dry-run and the kill switch both refuse action.

    ``scope="storage"`` honors the storage kill switch and watch-only setting.
    ``scope="github"`` is for draft/notification agency (same dry-run / MCP
    rules, no storage kill switch).
    """
    if scope == "storage" and not storage_enabled(settings_get):
        return False, "kill switch: hygiene.storage_enabled is false"
    if params.get("dry_run") is True or params.get("watch_only") is True:
        return False, "dry-run"
    surface = str(params.get("surface") or "")
    if surface in {"mcp", "agent_tool"} and not params.get("enforce"):
        return False, "MCP/agent surface stays dry-run unless enforce is set"
    if params.get("enforce") is True:
        return True, "enforce"
    if scope == "storage" and storage_watch_only(settings_get):
        return False, "hygiene.storage_watch_only is true"
    return True, "default agency"


def blast_caps(reading_pct: float | None, settings_get: Callable[[str, Any], Any] | None = None, *, aggressive: bool = False) -> BlastCaps:
    if aggressive or (reading_pct is not None and reading_pct >= 85):
        items = int(_settings(settings_get, "hygiene.storage_aggressive_max_items", AGGRESSIVE_MAX_ITEMS) or AGGRESSIVE_MAX_ITEMS)
        gib = float(_settings(settings_get, "hygiene.storage_aggressive_max_gib", AGGRESSIVE_MAX_GIB) or AGGRESSIVE_MAX_GIB)
    else:
        items = int(_settings(settings_get, MAX_ITEMS_SETTING, DEFAULT_MAX_ITEMS) or DEFAULT_MAX_ITEMS)
        gib = float(_settings(settings_get, MAX_GIB_SETTING, DEFAULT_MAX_GIB) or DEFAULT_MAX_GIB)
    return BlastCaps(max_items=max(0, items), max_gib=max(0.0, gib))


def apply_blast_caps(items: Sequence[SizedItem], caps: BlastCaps) -> tuple[list, list]:
    """Split (within_caps, deferred) without exceeding max items or bytes.

    Items should already be policy-approved. Prefer larger items first so a
    tight cap still moves the storage needle.
    """
    ordered = sorted(items, key=lambda it: int(getattr(it, "size_bytes", 0) or 0), reverse=True)
    chosen: list = []
    deferred: list = []
    total = 0
    for item in ordered:
        size = int(getattr(item, "size_bytes", 0) or 0)
        if len(chosen) >= caps.max_items or total + size > caps.max_bytes:
            deferred.append(item)
            continue
        chosen.append(item)
        total += size
    return chosen, deferred


def _rate_path(state_dir: Path) -> Path:
    return Path(state_dir) / "agents" / "tidy" / "storage-rate.json"


def rate_limit_allows(
    state_dir: Path,
    *,
    now: datetime,
    max_runs_per_hour: int | None = None,
    settings_get: Callable[[str, Any], Any] | None = None,
) -> tuple[bool, str]:
    """True when another enforce run is allowed in the trailing hour."""
    cap = max_runs_per_hour
    if cap is None:
        cap = int(_settings(settings_get, MAX_RUNS_SETTING, DEFAULT_MAX_RUNS_PER_HOUR) or DEFAULT_MAX_RUNS_PER_HOUR)
    if cap <= 0:
        return False, "rate limit: max_runs_per_hour is 0"
    path = _rate_path(state_dir)
    stamps = _read_stamps(path)
    cutoff = now - timedelta(hours=1)
    recent = [s for s in stamps if s >= cutoff]
    if len(recent) >= cap:
        return False, f"rate limit: {len(recent)} enforce runs in the last hour (cap {cap})"
    return True, "ok"


def record_enforce_run(state_dir: Path, now: datetime) -> None:
    path = _rate_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamps = _read_stamps(path)
    stamps.append(now)
    cutoff = now - timedelta(hours=24)
    stamps = [s for s in stamps if s >= cutoff]
    path.write_text(json.dumps([s.isoformat() for s in stamps]) + "\n", encoding="utf-8")


def _read_stamps(path: Path) -> list[datetime]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[datetime] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        try:
            out.append(datetime.fromisoformat(str(item).replace("Z", "+00:00")))
        except ValueError:
            continue
    return out


def file_needs_you(
    *,
    summary: str,
    decision: str,
    detail: str = "",
    recipients: tuple[str, ...],
    dedup_key: str,
    sender: Callable | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Put a Tier-2 ask in Needs you: HERALD inbox row + a held approval write."""
    from axiom.extensions.builtins.hygiene.storage_alerts import Alert, send_alerts

    alert = Alert(
        kind="needs_you",
        severity="urgent",
        summary=summary,
        action_taken="Held this for your approval (TIDY will not do it on its own).",
        decision=decision,
        dedup_key=dedup_key,
        detail=detail,
    )
    sent = send_alerts([alert], recipients, already_sent=set(), sender=sender) if recipients else []
    action_id = None
    if state_dir is not None:
        try:
            from axiom.infra.orchestrator.actions import ActionCategory, create_action
            from axiom.infra.orchestrator.approval import ApprovalGate
            from axiom.infra.orchestrator.approval_store import FileActionStore

            store = FileActionStore(Path(state_dir) / "orchestrator" / "approvals.json")
            gate = ApprovalGate(store)
            action = create_action(
                "hygiene.tier2",
                {"summary": summary, "decision": decision, "detail": detail, "dedup_key": dedup_key},
            )
            action.category = ActionCategory.WRITE
            stored = gate.submit(action)
            action_id = stored.action_id
        except Exception as exc:  # noqa: BLE001 — HERALD alone still lands in Needs you
            _log.warning("could not file approval-gate write: %s", exc)
    return {"alert": alert.to_dict(), "sent": sent, "action_id": action_id}


__all__ = [
    "AGGRESSIVE_MAX_GIB",
    "AGGRESSIVE_MAX_ITEMS",
    "BlastCaps",
    "DEFAULT_MAX_GIB",
    "DEFAULT_MAX_ITEMS",
    "RECOVERY_ARTIFACT",
    "RECOVERY_CACHE",
    "apply_blast_caps",
    "blast_caps",
    "decide_act",
    "file_needs_you",
    "rate_limit_allows",
    "record_enforce_run",
    "storage_enabled",
    "storage_watch_only",
]
