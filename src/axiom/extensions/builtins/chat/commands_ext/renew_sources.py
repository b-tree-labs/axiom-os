# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where `/renew` can find sessions besides the chat's own: other harnesses on this machine.

A person with the Axiom MCP installed in several harnesses (Claude Code, Codex, OpenCode, ...) has conversations
in each. A reader ships for every harness whose format was verified from a real install or real files (the table
and the rules are in ``axiom.memory.absorb.sessions``; what is known about the rest, and why they have none, is
``axiom.memory.absorb.session_status``). `/renew` lists them together with the chat's native sessions, labelled by SOURCE, and `/renew <harness>`
narrows the list to one. This module owns the parts that are not drawing or resolving:

* which harness names are real options (the Axiom MCP is registered in them, or a session reader exists for
  them), and how to say so when one has no reader yet;
* the read-only readers (``axiom.memory.absorb.sessions``) turned into picker rows, secret-safe;
* the scope rule: another harness's files are this machine's owner's, so only the machine's own principal sees
  them. A served session for anybody else lists none;
* the import: a foreign session becomes a NEW native session carrying ``context["renewed_from"]`` provenance
  (SourceOrigin-style, ADR-087 D1). The source is never touched; importing the same source again updates the
  same native session (idempotent by ``(harness, source_session_id)``) and keeps whatever was said after it.
* the tier hold: imported content has no classification, and cross-mem's rule is that unlabeled is not public,
  so the session may not be sent to a cloud or remote provider until the person confirms it.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NATIVE = "chat"
PROVENANCE_KEY = "renewed_from"

#: Other spellings that name the same place.
ALIASES = {"gemini-cli": "gemini", "claude": "claude-code", "native": "chat", "axiom": "chat"}

def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def foreign_home() -> Path:
    """Where the other harnesses keep their files. ``AXIOM_FOREIGN_SESSIONS_HOME`` points the readers
    elsewhere (a test, a mounted backup); otherwise it is the user's home."""
    override = os.environ.get("AXIOM_FOREIGN_SESSIONS_HOME")
    return Path(override) if override else Path.home()


def readers() -> dict[str, Any]:
    from axiom.memory.absorb.session_index import SessionIndexCache
    from axiom.memory.absorb.sessions import session_readers

    # The header cache only makes listing fast; any trouble with it means no cache, never a failed listing.
    try:
        cache = SessionIndexCache()
    except Exception:  # noqa: BLE001
        cache = None
    return session_readers(foreign_home(), cache=cache)


def registered_harnesses() -> list[str]:
    """Harnesses with the Axiom MCP in their config, read from the config files themselves (never a shell-out)."""
    try:
        from axiom.extensions.builtins.mcp.harnesses import survey

        return sorted({v.tool for v in survey()})
    except Exception:  # noqa: BLE001 - completion and listing must never raise
        return []


def _spec_names() -> list[str]:
    try:
        from axiom.extensions.builtins.mcp.install import supported_tools

        return list(supported_tools())
    except Exception:  # noqa: BLE001
        return []


def canonical(token: str) -> str | None:
    """The harness name ``token`` means, or None if it is not a harness name at all."""
    name = ALIASES.get(token.strip().lower(), token.strip().lower())
    from axiom.memory.absorb.session_status import HARNESS_STATUS

    known = {NATIVE, *readers(), *HARNESS_STATUS, *_spec_names()}
    return name if name in known else None


@dataclass(frozen=True)
class HarnessOption:
    name: str
    registered: bool  # the Axiom MCP is installed there
    has_reader: bool

    @property
    def description(self) -> str:
        if self.name == NATIVE:
            return "this chat's own saved sessions"
        if self.has_reader:
            return "sessions from " + self.name + (" (Axiom MCP installed)" if self.registered else "")
        return "Axiom MCP installed; no session reader yet"


def harness_options() -> list[HarnessOption]:
    """What `/renew <harness>` completes to: the chat itself, then the union of harnesses with the MCP
    registered and harnesses with a session reader."""
    have = set(readers())
    registered = set(registered_harnesses())
    options = [HarnessOption(NATIVE, False, True)]
    for name in sorted(have | registered):
        options.append(HarnessOption(name, name in registered, name in have))
    return options


def reader_names() -> list[str]:
    return sorted(readers())


def no_reader_message(harness: str) -> str:
    """Said when a harness is chosen that has no session reader: what is missing and what exists."""
    from axiom.memory.absorb.session_status import HARNESS_STATUS

    have = [NATIVE, *reader_names()]
    status = HARNESS_STATUS.get(harness)
    registered = harness in registered_harnesses()
    lead = (
        f"the Axiom MCP is installed in {harness}, but /renew has no session reader for it yet"
        if registered
        else f"/renew has no session reader for {harness} yet"
    )
    out = f"\n  {harness}: {lead}, so its sessions cannot be listed or renewed.\n"
    if status and status.missing:
        out += f"  What is missing: {status.missing}.\n"
    if status and status.exists:
        out += f"  What exists for it: {status.exists}.\n"
    elif not status:
        out += "  What exists for it: nothing that reads its conversations.\n"
    if status:
        known = f" ({status.version})" if status.version else ""
        out += f"  Format checked: {status.verified_by}{known}.\n"
    out += f"  Sources that work now: {', '.join(have)}. Try /renew <source>.\n"
    return out


def explain_empty(source: str) -> str:
    """Why a source that HAS a reader listed nothing: a missing or unreadable directory, sessions it had to skip
    and the reason for each, and any part of that harness's history that is not read. ``""`` when there is
    nothing to add."""
    from axiom.memory.absorb.session_status import READER_GAPS

    reader = readers().get(source)
    lines: list[str] = []
    if reader is not None:
        try:
            reader.list_sessions(limit=50)
        except Exception:  # noqa: BLE001
            return ""
        lines += [f"  {n}" for n in reader.notes[:3]]
        for sk in reader.skipped[:3]:
            lines.append(f"  skipped {Path(sk.source).name}: {sk.reason}")
        if len(reader.skipped) > 3:
            lines.append(f"  ... and {len(reader.skipped) - 3} more skipped")
    gap = READER_GAPS.get(source)
    if gap:
        lines.append(f"  Not read: {gap}.")
    return "\n".join(lines) + "\n" if lines else ""


# ---- who may see foreign sessions ---------------------------------------------------------------


def may_read_foreign(principal: str) -> bool:
    """Other harnesses' files belong to the person at this machine. Only the machine's own principal sees
    them; a session for anybody else (a served surface, another account) lists none."""
    if not principal:
        return False
    from axiom.infra.orchestrator import session as session_mod

    return principal == session_mod._default_principal() or session_mod.may_adopt(principal)


# ---- rows ---------------------------------------------------------------------------------------


def _looks_secret(text: str) -> bool:
    from axiom.memory.absorb.sessions import _looks_secret as detect

    return detect(text)


def _safe_text(text: str, fallback: str) -> str:
    return fallback if _looks_secret(text) else text


def _epoch(value: str) -> float:
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def foreign_rows(
    principal: str, source: str | None = None, *, limit: int = 50, imported: dict[tuple[str, str], str] | None = None
) -> list[dict[str, Any]]:
    """Rows for other harnesses' sessions: title and counts only, never message content. Titles that read as
    secrets are withheld."""
    if not may_read_foreign(principal) or source == NATIVE:
        return []
    imported = imported or {}
    rows: list[dict[str, Any]] = []
    for name, reader in readers().items():
        if source and name != source:
            continue
        try:
            headers = reader.list_sessions(limit=limit)
        except Exception:  # noqa: BLE001 - one unreadable harness must not hide the rest
            continue
        for h in headers:
            rows.append(
                {
                    "id": f"{name}:{h.source_session_id}",
                    "ref": h.source_session_id,
                    "short": h.source_session_id[:8],
                    "source": name,
                    "title": _safe_text(h.title, "(title withheld: reads as a secret)"),
                    "message_count": h.message_count,
                    "created_at": h.created_at,
                    "updated_at": h.updated_at,
                    "archived": False,
                    "tier": "unclassified",
                    "provider": "",
                    "model": "",
                    "workspace": Path(h.cwd).name if h.cwd else "",
                    "imported_as": imported.get((name, h.source_session_id), ""),
                }
            )
    return rows


def merge_newest_first(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    # Newest first by the harness's last-activity stamp; an unparsable or missing stamp counts as oldest and
    # ties break by id, so the order is deterministic whatever the clocks said.
    return sorted(rows, key=lambda r: (-_epoch(r.get("updated_at", "")), str(r.get("id", ""))))[:limit]


def split_id(session_id: str) -> tuple[str, str] | None:
    """``"codex:abc"`` -> ``("codex", "abc")`` when it names a harness with a reader, else None."""
    if ":" not in session_id:
        return None
    head, _, rest = session_id.partition(":")
    return (head, rest) if head in readers() and rest else None


# ---- importing ----------------------------------------------------------------------------------


def _path_hash(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


def find_imported(store: Any, principal: str, site: str, tenant: str, harness: str, source_id: str) -> str:
    """The native session already imported from this source, or ``""``."""
    from .renew import may_see

    for sid in store.list_sessions(include_archived=True):
        raw = store.peek(sid)
        if not raw or not may_see(raw, principal, site, tenant):
            continue
        prov = (raw.get("context") or {}).get(PROVENANCE_KEY) or {}
        if prov.get("harness") == harness and prov.get("source_session_id") == source_id:
            return sid
    return ""


def import_session(store: Any, agent: Any, harness: str, source_id: str) -> tuple[str, str]:
    """Create or refresh the native session for a foreign one. Returns ``(native_session_id, "")`` or
    ``("", why)``. The source is only read."""
    from axiom.infra.orchestrator.session import Message, Session

    from .renew import _scope_of

    current = getattr(agent, "session", None)
    principal, site, tenant = _scope_of(current)
    if not may_read_foreign(principal):
        return "", f"Session not found: {harness}:{source_id}"
    reader = readers().get(harness)
    transcript = reader.load_session(source_id) if reader is not None else None
    if transcript is None or not transcript.messages:
        why = getattr(reader, "last_error", "") if reader is not None else ""
        return "", f"Session not found: {harness}:{source_id}" + (f" ({why})" if why else "")

    existing_id = find_imported(store, principal, site, tenant, harness, source_id)
    if existing_id and current is not None and current.session_id == existing_id:
        store.save(current)  # what was said since the last import is on disk before it is read back
    previous = store.load(existing_id, principal=principal) if existing_id else None

    now = datetime.now(UTC).isoformat()
    converted = [Message(m.role, m.content, m.timestamp or now) for m in transcript.messages]
    tail: list[Any] = []
    confirmed = False
    prior = ((previous.context.get(PROVENANCE_KEY) or {}) if previous else {}) or {}
    if previous is not None:
        tail = list(previous.messages[int(prior.get("message_count", len(previous.messages))) :])
        # A confirmation covers the content that was confirmed: changed content needs it again.
        confirmed = bool(prior.get("tier_confirmed")) and int(prior.get("message_count", -1)) == len(converted)

    secret_like = sum(1 for m in transcript.messages if _looks_secret(m.content))
    provenance = {
        "harness": harness,
        "source_session_id": source_id,
        "source_path_hash": _path_hash(transcript.header.path),
        "format_version": transcript.header.format_version,
        "imported_at": now,
        "message_count": len(converted),
        "tier": "unclassified",
        "tier_confirmed": confirmed,
        "lossy": dict(transcript.lossy),
        "secret_like_messages": secret_like,
        "refreshed": previous is not None,
        "kept_after_import": len(tail),
    }
    session = previous or Session(principal_id=principal, site_id=site, tenant_id=tenant)
    session.title = transcript.header.title and _safe_text(transcript.header.title, "(imported session)") or (
        f"{harness} session {source_id[:8]}"
    )
    session.messages = converted + tail
    session.context[PROVENANCE_KEY] = provenance
    session.updated_at = now
    store.save(session)
    return session.session_id, ""


# ---- the tier hold ------------------------------------------------------------------------------


def provenance_of(session: Any) -> dict[str, Any]:
    ctx = getattr(session, "context", None) or {}
    prov = ctx.get(PROVENANCE_KEY)
    return prov if isinstance(prov, dict) else {}


def tier_held(session: Any) -> bool:
    prov = provenance_of(session)
    return bool(prov) and not prov.get("tier_confirmed")


def send_refusal(session: Any, gateway: Any) -> str:
    """A refusal when this turn could reach a cloud or remote provider while the session holds unconfirmed
    imported content; ``""`` when it may proceed. Every provider the gateway could fall back to must be local:
    a hold that only checked the first choice would leak on the first failure."""
    if not tier_held(session):
        return ""
    providers = list(getattr(gateway, "providers", None) or [])
    if not providers:
        return ""
    from axiom.llm.placement import placement_of

    # Every provider the gateway could use or fall back to must be local. A provider whose placement cannot be
    # worked out counts as outside: unknown is not local.
    outside = []
    for p in providers:
        if not (getattr(p, "api_key", "") or not getattr(p, "api_key_env", "")):
            continue  # unusable: no credential, so nothing can be sent through it
        try:
            local = placement_of(p).placement == "local"
        except Exception:  # noqa: BLE001
            local = False
        if not local:
            outside.append(p.name)
    if not outside:
        return ""
    prov = provenance_of(session)
    return (
        f"\n  Not sent. This session was imported from {prov.get('harness', 'another harness')} and its content "
        f"has no classification, so it is treated as unknown and may not go to a cloud or remote provider "
        f"({', '.join(outside)}).\n"
        "  Run /renew confirm-tier to say it holds nothing restricted, or switch to a local provider with /model.\n"
    )


def confirm_tier(agent: Any, store: Any) -> str:
    session = getattr(agent, "session", None)
    prov = provenance_of(session)
    if not prov:
        return "\n  Nothing to confirm: this session was not imported from another harness.\n"
    if prov.get("tier_confirmed"):
        return "\n  Already confirmed: this imported content may go to any provider the session's tier allows.\n"
    prov["tier_confirmed"] = True
    session.context[PROVENANCE_KEY] = prov
    try:
        store.save(session)
    except Exception:  # noqa: BLE001 - the confirmation holds for this run even if the save fails
        pass
    return (
        f"\n  Confirmed: you have said the content imported from {prov.get('harness')} holds nothing restricted. "
        "It may now go to cloud and remote providers. Run /where to see where the next message will go.\n"
    )


def lossy_line(prov: dict[str, Any]) -> str:
    lossy = prov.get("lossy") or {}
    bits = []
    if lossy.get("tool_calls") or lossy.get("tool_results"):
        bits.append(
            f"tool calls kept as text summaries ({lossy.get('tool_calls', 0)} calls, "
            f"{lossy.get('tool_results', 0)} results with content not imported)"
        )
    if lossy.get("thinking_dropped"):
        bits.append(f"{lossy['thinking_dropped']} reasoning blocks dropped")
    if lossy.get("attachments_dropped"):
        bits.append(f"{lossy['attachments_dropped']} attachments dropped")
    for key, text in (
        ("older_messages_omitted", "{n} older messages left out (the import keeps the newest {cap})"),
        ("long_messages_truncated", "{n} very long messages cut"),
        ("oversize_lines_skipped", "{n} oversize records skipped"),
        ("invalid_lines", "{n} unreadable records skipped"),
        ("partial_trailing_line", "the last record was still being written and was left out"),
        ("unknown_blocks", "{n} content blocks of an unknown kind left out"),
    ):
        if lossy.get(key):
            from axiom.memory.absorb import sessions as _s

            bits.append(text.format(n=lossy[key], cap=_s.MAX_MESSAGES))
    if lossy.get("harness_context_dropped"):
        bits.append(f"{lossy['harness_context_dropped']} harness-injected context blocks dropped")
    return "converted: roles kept; " + "; ".join(bits) if bits else ""
