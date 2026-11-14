# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Platform events: the machine's own log, read by ``<cli> log platform <verb>`` (ADR-243).

Each verb is a skill (ADR-056); the command line only parses flags and prints. Platform
events are not a signed log and have no write verb.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillResult
from axiom.infra.time_utils import parse_iso

#: At most this many routing events are returned by ``tail``, however many are asked for.
TAIL_LIMIT = 1000

_LEVEL_MAP = {"debug": 10, "info": 20, "warning": 30, "warn": 30, "error": 40, "critical": 50}


def _fail(msg: str) -> SkillResult:
    return SkillResult(ok=False, errors=[msg])


def iter_jsonl(path: Path) -> Iterator[dict]:
    """Parsed JSON objects from a JSONL file, skipping malformed lines."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def routing_events_path() -> Path | None:
    """The routing_events JSONL file, from the audit log or the standard runtime location."""
    try:
        from axiom.infra.audit_log import AuditLog

        al = AuditLog.get()
        if hasattr(al, "routing_events_path"):
            return Path(al.routing_events_path)  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        pass
    try:
        from axiom import REPO_ROOT

        candidate = Path(REPO_ROOT) / "runtime" / "logs" / "routing_events.jsonl"
        if candidate.exists():
            return candidate
    except (ImportError, OSError):
        pass
    return None


def level_no(name: str) -> int:
    return _LEVEL_MAP.get(str(name or "").lower(), 0)


def _parse_ts(record: dict) -> datetime | None:
    raw = record.get("ts") or record.get("timestamp") or record.get("time")
    if not raw:
        return None
    try:
        return parse_iso(str(raw))
    except ValueError:
        return None


def _provider(record: dict) -> str:
    return record.get("llm_provider") or record.get("provider") or record.get("provider_name") or ""


def matches(record: dict, params: dict[str, Any]) -> bool:
    """Whether a routing event passes ``tail``'s filters (``ec``, ``provider``, ``level``)."""
    if params.get("ec") and not (record.get("ec_violation") or record.get("is_ec")):
        return False
    if params.get("provider") and _provider(record) != params["provider"]:
        return False
    if params.get("level"):
        rec_level = record.get("levelno") or level_no(record.get("level", ""))
        if rec_level < level_no(params["level"]):
            return False
    return True


def format_record(record: dict) -> str:
    ts = record.get("ts") or record.get("timestamp") or record.get("time") or "?"
    tier = record.get("tier") or record.get("route") or record.get("routing_tier") or "?"
    blocked = record.get("blocked", record.get("is_blocked", "?"))
    return f"{ts} | {_provider(record) or '?'} | {tier} | {blocked}"


def _read_after(path: Path, offset: int) -> tuple[list[dict], int]:
    """Events written after byte ``offset`` (a whole line at a time), and the new offset."""
    out: list[dict] = []
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # a line still being written is read on the next call
            offset += len(raw)
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out, offset


def tail(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """The last ``n`` routing events that pass the filters, and the byte offset the file ends at.

    With ``after`` (an offset a previous call returned), only events written since, up to
    ``TAIL_LIMIT``: what ``tail --follow`` polls.
    """
    path = routing_events_path()
    if path is None:
        return _fail("routing_events log file not found")
    n = max(1, min(int(params.get("n") or 20), TAIL_LIMIT))
    if params.get("after") is not None:
        fresh, end = _read_after(path, int(params["after"]))
        kept = [r for r in fresh if matches(r, params)][-TAIL_LIMIT:]
        return SkillResult(ok=True, value={"path": str(path), "end": end, "records": kept})
    kept: list[dict] = []
    for rec in iter_jsonl(path):
        if matches(rec, params):
            kept.append(rec)
            if len(kept) > n:
                kept.pop(0)
    try:
        end = path.stat().st_size
    except OSError:
        end = 0
    return SkillResult(ok=True, value={"path": str(path), "end": end, "records": kept})


def verify(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Check the audit log's hash chain; ``ok`` False names where it broke."""
    try:
        from axiom.infra.audit_log import AuditLog

        result = AuditLog.get().verify_chain(table=params.get("table"))
    except (ImportError, AttributeError, TypeError) as e:
        return _fail(f"error calling AuditLog.verify_chain: {e}")
    if isinstance(result, dict):
        ok = bool(result.get("ok", result.get("valid", True)))
        broken = result.get("broken_at") if result.get("broken_at") is not None else result.get("index")
        return SkillResult(ok=True, value={"ok": ok, "broken_at": None if ok else broken})
    if isinstance(result, bool):
        return SkillResult(ok=True, value={"ok": result, "broken_at": None})
    return SkillResult(ok=True, value={"ok": True, "broken_at": None, "detail": str(result)})


def _backend_name() -> str:
    from axiom.infra.audit_log import AuditLog

    al = AuditLog.get()
    info = al.backend_info() if hasattr(al, "backend_info") else {}
    return getattr(info, "backend", None) or str(type(al).__name__)


def backend(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """The audit log backend's name."""
    try:
        return SkillResult(ok=True, value={"name": _backend_name()})
    except (ImportError, AttributeError, TypeError) as e:
        return _fail(str(e))


def sinks(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """The active log sinks."""
    try:
        from axiom.infra.log_sinks import LogSinkFactory

        found = LogSinkFactory.available()
    except (ImportError, AttributeError, TypeError) as e:
        return _fail(str(e))
    return SkillResult(ok=True, value={"sinks": [str(s) for s in found or []]})


def stats(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """The backend, line counts of the runtime JSONL files, and EC and blocked counts."""
    try:
        name = _backend_name()
    except (ImportError, AttributeError, TypeError) as e:
        name = f"(unavailable: {e})"
    files: dict[str, int] | None = None
    try:
        from axiom import REPO_ROOT

        logs_dir = Path(REPO_ROOT) / "runtime" / "logs"
        if logs_dir.is_dir():
            files = {f.name: sum(1 for _ in iter_jsonl(f)) for f in sorted(logs_dir.glob("*.jsonl"))}
    except Exception:  # noqa: BLE001 - a missing runtime dir is reported as such
        files = None
    routing = None
    path = routing_events_path()
    if path:
        ec = blocked = 0
        for rec in iter_jsonl(path):
            ec += bool(rec.get("ec_violation") or rec.get("is_ec"))
            blocked += bool(rec.get("blocked") or rec.get("is_blocked"))
        routing = {"ec_violations": ec, "blocked": blocked}
    return SkillResult(ok=True, value={"backend": name, "files": files, "routing_events": routing})


def export(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Every routing event, or those of the last ``since`` days, in file order."""
    path = routing_events_path()
    if path is None:
        return _fail("routing_events log file not found")
    since = params.get("since")
    cutoff = datetime.now(UTC) - timedelta(days=int(since)) if since is not None else None
    out = []
    for rec in iter_jsonl(path):
        if cutoff is not None:
            ts = _parse_ts(rec)
            if ts is not None and ts < cutoff:
                continue
        out.append(rec)
    return SkillResult(ok=True, value={"records": out})


__all__ = [
    "TAIL_LIMIT",
    "backend",
    "export",
    "format_record",
    "iter_jsonl",
    "matches",
    "routing_events_path",
    "sinks",
    "stats",
    "tail",
    "verify",
]
