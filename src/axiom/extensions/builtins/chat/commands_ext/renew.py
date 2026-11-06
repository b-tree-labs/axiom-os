# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`/renew` (and `/resume`, the same handler): pick a previous session from a list and carry on.

The shape is the one people already know from Claude Code's resume picker: most recent first, each row
saying what the session was (title), when it was last used, how long it is, and where it ran; type to
narrow the list, Enter takes the highlighted one, Esc cancels. The full-screen picker is the chat's
existing session picker (``fullscreen._session_chooser`` over ``chooser.py``); this module owns the
parts that are not drawing:

* WHICH sessions a person may see (the store's owner scope: principal, site, tenant), so no surface
  lists or resumes somebody else's conversation;
* how a typed query (number, id prefix, title text) resolves to one session;
* the line-REPL list-and-choose interaction;
* what resuming does: load the messages, restore the per-session settings, and say what the person is
  renewing, including the case where the provider now active would not be admitted for the content the
  session already holds.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from ..command_registry import REGISTRY, CliSpec, Command, CommandContext
from ..follow_ups import FollowUp

CONTROLLED = "export_controlled"
SOURCE_NATIVE = "chat"  # a session this chat itself holds; any other source is a harness it was read from

#: Session context keys this module reads and writes. ``rag`` and ``trace`` are written by /rag and /trace.
LAST_PROVIDER_KEY = "last_provider"
LAST_MODEL_KEY = "last_model"
MODE_KEY = "mode"  # ask | plan | agent
ROUTING_MODE_KEY = "session_mode"  # the routing tier hint (--mode)

_ROW_LIMIT = 200
_COMPLETE_LIMIT = 20


# ---- who may see what ---------------------------------------------------------------------------


def principal_of(session: Any) -> str:
    value = getattr(session, "principal_id", "")
    return value if isinstance(value, str) else ""


def _scope_of(session: Any) -> tuple[str, str, str]:
    def text(name: str) -> str:
        value = getattr(session, name, "")
        return value if isinstance(value, str) else ""

    return principal_of(session), text("site_id"), text("tenant_id")


def may_see(raw: dict[str, Any], principal: str, site: str = "", tenant: str = "") -> bool:
    """Whether a stored session record is visible in the given scope.

    The principal rule is the store's own (own sessions, sessions that predate ownership, and this
    node's legacy handles for its own human); site and tenant are the same shape: a record that names
    one must match the asker's. A record naming none predates scoping and stays visible.
    """
    from axiom.infra.orchestrator.session import may_adopt, node_legacy_handles

    owner = raw.get("principal_id", "") or ""
    if owner:
        if not principal:
            return False
        if owner != principal and not (owner in node_legacy_handles() and may_adopt(principal)):
            return False
    for key, mine in (("site_id", site), ("tenant_id", tenant)):
        theirs = raw.get(key, "") or ""
        if theirs and theirs != mine:
            return False
    return True


def _row_from(raw: dict[str, Any]) -> dict[str, Any]:
    ctx = raw.get("context") or {}
    messages = raw.get("messages") or []
    return {
        "id": raw.get("session_id", ""),
        "title": raw.get("title", "") or "",
        "message_count": len(messages),
        "created_at": raw.get("created_at", "") or "",
        "updated_at": raw.get("updated_at", "") or raw.get("created_at", "") or "",
        "archived": bool(raw.get("archived")),
        "tier": str(ctx.get("content_tier", "any")),
        "provider": str(ctx.get(LAST_PROVIDER_KEY, "") or ""),
        "model": str(ctx.get(LAST_MODEL_KEY, "") or ""),
        "workspace": str(ctx.get("workspace", "") or ctx.get("branch", "") or ""),
        "source": SOURCE_NATIVE,
        "renewed_from": str((ctx.get("renewed_from") or {}).get("harness", "") or ""),
    }


def session_rows(
    store: Any, session: Any, *, include_archived: bool = False, limit: int = _ROW_LIMIT
) -> list[dict[str, Any]]:
    """The sessions ``session``'s owner may renew, most recent first (archived ones only on request).

    Read-only: it never loads a session through ``store.load`` (which would un-archive).
    """
    return rows_for(store, *_scope_of(session), include_archived=include_archived, limit=limit)


def rows_for(
    store: Any,
    principal: str,
    site: str = "",
    tenant: str = "",
    *,
    include_archived: bool = False,
    limit: int = _ROW_LIMIT,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sid in store.list_sessions(include_archived=include_archived):
        raw = store.peek(sid)
        if raw is None or not may_see(raw, principal, site, tenant):
            continue
        if raw.get("archived") and not include_archived:
            continue
        raw.setdefault("session_id", sid)
        rows.append(_row_from(raw))
        if len(rows) >= limit:
            break
    return rows


def aggregate_rows(
    store: Any,
    session: Any,
    *,
    source: str | None = None,
    include_archived: bool = False,
    limit: int = _ROW_LIMIT,
) -> list[dict[str, Any]]:
    """Native sessions AND other harnesses' sessions in one list, newest first, each row labelled with its
    ``source``; ``source`` narrows it (``chat`` is the native ones). Other harnesses' sessions are listed
    only to the person whose machine they are on."""
    from . import renew_sources as src

    principal, site, tenant = _scope_of(session)
    native = rows_for(store, principal, site, tenant, include_archived=include_archived, limit=limit)
    imported = {(r["renewed_from"], r["_ref"]): r["id"] for r in _imported_refs(store, native)}
    foreign = src.foreign_rows(principal, source, limit=min(limit, 50), imported=imported)
    shown = native if source in (None, SOURCE_NATIVE) else []
    return src.merge_newest_first(shown + foreign, limit)


def _imported_refs(store: Any, native: list[dict[str, Any]]):
    """``{renewed_from, _ref, id}`` for native sessions that were imported, so a foreign row can say it is."""
    out = []
    for r in native:
        if not r.get("renewed_from"):
            continue
        raw = store.peek(r["id"]) or {}
        prov = (raw.get("context") or {}).get("renewed_from") or {}
        out.append({"renewed_from": r["renewed_from"], "_ref": prov.get("source_session_id", ""), "id": r["id"]})
    return out


@dataclass(frozen=True)
class Request:
    query: str = ""
    include_archived: bool = False
    source: str | None = None
    as_json: bool = False
    bad_source: str = ""  # a --harness value that names nothing


def parse_request(args: list[str]) -> Request:
    """``[--all] [--json] [--harness NAME | --source NAME | NAME] [number|id|text]``. A leading word that is a
    harness name is the source; use ``--source`` when a title happens to be one."""
    from . import renew_sources as src

    rest: list[str] = []
    source: str | None = None
    bad = ""
    include_archived = "--all" in args
    as_json = "--json" in args
    it = iter(a for a in args if a not in ("--all", "--json"))
    for a in it:
        name = None
        if a in ("--harness", "--source"):
            name = next(it, "")
        elif a.startswith(("--harness=", "--source=")):
            name = a.split("=", 1)[1]
        if name is not None:
            found = src.canonical(name)
            if found:
                source = found
            else:
                bad = name
            continue
        rest.append(a)
    if source is None and not bad and rest:
        found = src.canonical(rest[0])
        if found and rest[0].lower() not in ("confirm-tier",):
            source, rest = found, rest[1:]
    return Request(" ".join(rest).strip(), include_archived, source, as_json, bad)


# ---- turning a typed query into one session -----------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    rows: tuple[dict[str, Any], ...]  # the matches: exactly one means "resume it"
    problem: str = ""  # set when nothing matched, or the number was out of range

    @property
    def unique(self) -> dict[str, Any] | None:
        return self.rows[0] if len(self.rows) == 1 and not self.problem else None


def _is_subsequence(needle: str, hay: str) -> bool:
    it = iter(hay)
    return all(ch in it for ch in needle)


def title_matches(rows: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Rows whose title matches ``text``: every word contained, else (only if none) the letters in order."""
    words = text.lower().split()
    if not words:
        return list(rows)
    exact = [r for r in rows if all(w in (r["title"] or "").lower() for w in words)]
    if exact:
        return exact
    squashed = "".join(words)
    return [r for r in rows if _is_subsequence(squashed, (r["title"] or "").lower().replace(" ", ""))]


def filter_rows(rows: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """What the picker keeps as the person types: title matches, else rows whose id starts with the text."""
    text = text.strip()
    if not text:
        return list(rows)
    return (
        title_matches(rows, text)
        or [r for r in rows if r["id"].startswith(text)]
        or [r for r in rows if r.get("source", "").startswith(text.lower())]
    )


def resolve(rows: list[dict[str, Any]], token: str) -> Resolution:
    """A number from the displayed list, an id prefix, or text to match against titles."""
    token = token.strip()
    if not token:
        return Resolution((), "nothing to match")
    if token.isdigit():
        n = int(token)
        if 1 <= n <= len(rows):
            return Resolution((rows[n - 1],))
        if len(token) < 4:
            return Resolution((), f"no session number {token} (the list has {len(rows)})")
    by_id = [r for r in rows if r["id"].startswith(token) or r.get("ref", "").startswith(token)]
    if len(by_id) == 1:
        return Resolution(tuple(by_id))
    if len(by_id) > 1:
        return Resolution(tuple(by_id))
    by_title = title_matches(rows, token)
    if by_title:
        return Resolution(tuple(by_title))
    return Resolution((), f"no session matches {token!r}")


# ---- rendering ----------------------------------------------------------------------------------


def tier_badge(tier: str) -> str:
    if tier == CONTROLLED:
        return "[CONTROLLED]"
    if tier and tier not in ("any", "public"):
        return f"[{tier}]"
    return ""


def via_label(row: dict[str, Any]) -> str:
    """Tier badge and the provider last used, for the column that says what is being renewed."""
    tier = row.get("tier", "")
    badge = "[unclassified]" if tier == "unclassified" else tier_badge(tier)
    parts = [badge, row.get("provider", "") or ""]
    return " ".join(p for p in parts if p)


def _ago(value: str) -> str:
    if not value:
        return ""
    from axiom.infra.time_utils import time_ago

    return time_ago(value)


def render_rows(rows: list[dict[str, Any]], *, width: int = 130) -> list[str]:
    """A numbered list, one line per session."""
    from axiom.infra.cli_format import Column, table

    cells = []
    for i, r in enumerate(rows, 1):
        title = r["title"] or "(untitled)"
        if len(title) > 40:
            title = title[:37] + "..."
        notes = []
        via = via_label(r)
        if via:
            notes.append(via)
        if r.get("workspace"):
            notes.append(r["workspace"])
        if r.get("archived"):
            notes.append("(archived)")
        if r.get("renewed_from"):
            notes.append(f"(renewed from {r['renewed_from']})")
        if r.get("imported_as"):
            notes.append("(imported)")
        cells.append(
            (
                f"{i}.",
                r.get("source", SOURCE_NATIVE),
                r.get("short") or r["id"][:12],
                title,
                f"{r['message_count']} msgs",
                _ago(r["updated_at"]),
                "  ".join(notes),
            )
        )
    return table(
        cells,
        [
            Column(align="right"),
            Column(),
            Column(),
            Column(wrap=True),
            Column(align="right"),
            Column(),
            Column(wrap=True),
        ],
        width=width,
    )


# ---- resuming -----------------------------------------------------------------------------------


def remember_settings(agent: Any) -> None:
    """Record, on the session, what a renewal must restore: the provider that last answered, the
    interaction mode and the routing mode. Called after a completed turn; never raises."""
    try:
        session = agent.session
        ctx = session.context
        try:
            from ..where import where_report

            report = where_report(agent)
            if (
                isinstance(report.provider, str)
                and report.provider
                and isinstance(report.model, str)
                and report.basis == "answered"
            ):
                ctx[LAST_PROVIDER_KEY] = report.provider
                ctx[LAST_MODEL_KEY] = report.model
        except Exception:  # noqa: BLE001
            pass
        for key, value, default in (
            (MODE_KEY, getattr(agent, "_interaction_mode", ""), "agent"),
            (ROUTING_MODE_KEY, getattr(agent, "_session_mode", ""), "auto"),
        ):
            if isinstance(value, str) and value and value != default:
                ctx[key] = value
            else:
                ctx.pop(key, None)
    except Exception:  # noqa: BLE001 - bookkeeping must never cost a turn
        pass


def _restore_settings(agent: Any, session: Any) -> list[str]:
    ctx = getattr(session, "context", None) or {}
    restored: list[str] = []
    if ctx.get("rag") == "off":
        restored.append("retrieval off")
    if ctx.get("trace"):
        restored.append("trace on")
    mode = ctx.get(MODE_KEY)
    if isinstance(mode, str) and mode:
        try:
            agent.set_interaction_mode(mode)
            restored.append(f"mode {mode}")
        except Exception:  # noqa: BLE001 - an unknown stored mode is ignored, not fatal
            pass
    routing = ctx.get(ROUTING_MODE_KEY)
    if isinstance(routing, str) and routing:
        try:
            agent._session_mode = routing
            restored.append(f"routing {routing}")
        except Exception:  # noqa: BLE001
            pass
    return restored


def _tier_note(agent: Any, tier: str) -> str:
    """Say so when the provider now active may not hold this session's content. The gateway re-checks
    every request, so nothing controlled can reach it; this tells the person before they wonder."""
    if tier != CONTROLLED:
        return ""
    gateway = getattr(agent, "gateway", None)
    providers = list(getattr(gateway, "providers", None) or [])
    if not providers:
        return ""
    from axiom.llm.tier_policy import admits

    override = getattr(gateway, "_provider_override", None)
    active = next((p for p in providers if p.name == override), None) if override else None
    if active is None:
        active = getattr(gateway, "active_provider", None)
    if active is None or admits(active.routing_tier, tier):
        return ""
    options = [p.name for p in providers if admits(p.routing_tier, tier)]
    where = ", ".join(options) if options else "none is configured"
    return (
        f"\n  {active.name} (tier {active.routing_tier}) may not receive the {tier} content this "
        f"session holds, so it will not be used for it. Providers that may: {where}."
    )


def resume_session(store: Any, agent: Any, session_id: str) -> str:
    """Load ``session_id`` into ``agent`` if its owner may see it. Returns the text to show."""
    current = getattr(agent, "session", None)
    principal, site, tenant = _scope_of(current)
    from . import renew_sources as src

    foreign = src.split_id(session_id)
    if foreign is not None:
        # Another harness's session: bring it in as a native one (the source is only read), then renew that.
        session_id, why = src.import_session(store, agent, *foreign)
        if not session_id:
            return f"\n  {why}\n"
        current = getattr(agent, "session", None)
    peek = getattr(store, "peek", None)
    raw = peek(session_id) if callable(peek) else None
    if isinstance(raw, dict) and not may_see(raw, principal, site, tenant):
        # The same answer as a session that does not exist: do not confirm another person's id.
        return f"\n  Session not found: {session_id}\n"
    session = store.load(session_id, principal=principal) if principal else store.load(session_id)
    if session is None:
        return f"\n  Session not found: {session_id}\n"

    if current is not None and current is not session and current.session_id != session.session_id:
        try:
            store.save(current)
        except Exception:  # noqa: BLE001 - failing to save the old one must not strand the switch
            pass
    agent.session = session
    restored = _restore_settings(agent, session)

    ctx = getattr(session, "context", None) or {}
    title = getattr(session, "title", "") or "(untitled)"
    count = len(getattr(session, "messages", []) or [])
    used = _ago(getattr(session, "updated_at", "") or getattr(session, "created_at", ""))
    provider = str(ctx.get(LAST_PROVIDER_KEY, "") or "")
    last = "last used " + (used or "at an unknown time") + (f" on {provider}" if provider else "")
    prov = src.provenance_of(session)
    if prov:
        out = f"\n  renewed {title} from {prov.get('harness')} ({count} messages, imported, source unchanged)"
        if prov.get("refreshed"):
            kept = prov.get("kept_after_import", 0)
            out += "\n  this updated the earlier import of the same session" + (
                f"; the {kept} messages said since then were kept" if kept else ""
            )
        converted = src.lossy_line(prov)
        if converted:
            out += f"\n  {converted}"
        if prov.get("secret_like_messages"):
            out += (
                f"\n  {prov['secret_like_messages']} imported messages read like they hold a secret; they stay in "
                "this local session and are never shown in lists"
            )
        if not prov.get("tier_confirmed"):
            out += (
                "\n  Imported content has no classification, so it is treated as unknown: this session will not "
                "be sent to a cloud or remote provider until you run /renew confirm-tier (local providers work as usual)."
            )
    else:
        out = f"\n  renewed {title} ({count} messages, {last})"
    badge = tier_badge(str(ctx.get("content_tier", "any")))
    if badge and not prov:
        out += f" {badge}"
    if restored:
        out += f"\n  restored: {', '.join(restored)}"
    out += _tier_note(agent, str(ctx.get("content_tier", "any")))
    if prov:
        out += "\n  next: /where shows where the next message will go, /context what was loaded, /sessions the saved list"
    return out + "\n"


# ---- the interaction ----------------------------------------------------------------------------


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


def _read_line(prompt: str) -> str:
    return input(prompt)


def choose_in_repl(rows: list[dict[str, Any]], read=None, write=print) -> dict[str, Any] | None:
    """Numbered list, then read a choice: a number, or text to narrow the list; empty cancels."""
    read = read or _read_line
    shown = list(rows)
    for _ in range(20):
        write("")
        for line in render_rows(shown):
            write(line)
        write("")
        try:
            answer = read("  number to renew, text to filter, Enter to cancel> ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(shown):
            return shown[int(answer) - 1]
        narrowed = filter_rows(rows, answer)
        if not narrowed:
            write(f"  no session matches {answer!r}")
            continue
        if len(narrowed) == 1:
            return narrowed[0]
        shown = narrowed
    return None


def _ambiguous(rows: tuple[dict[str, Any], ...]) -> str:
    return (
        "\n  More than one session matches; nothing was resumed. Use a number or more of the id:\n"
        + "\n".join(render_rows(list(rows)))
        + "\n"
    )


def _listing(rows: list[dict[str, Any]], source: str | None) -> str:
    from . import renew_sources as src

    names = [src.NATIVE, *src.reader_names()]
    lines = [
        "",
        "  Sessions you can renew (most recent first" + (f", from {source}" if source else "") + "):",
        "",
        *render_rows(rows),
        "",
        f"  Renew one with {_cli_slash()} <number|id|text>.",
        f"  Only one source: {_cli_slash()} <{'|'.join(names)}>" + (f" (back to everything: {_cli_slash()})" if source else "") + ".",
        "  The chat's own sessions: /sessions.",
    ]
    waiting = [o.name for o in src.harness_options() if not o.has_reader]
    if waiting:
        lines.append(f"  Installed but no session reader yet: {', '.join(waiting)}.")
    return "\n".join(lines) + "\n"


def list_json(store: Any, session: Any, *, source: str | None = None, include_archived: bool = False) -> str:
    """The listing as JSON: titles and counts and where each came from, never message content."""
    import json

    rows = aggregate_rows(store, session, source=source, include_archived=include_archived)
    keys = ("id", "source", "title", "message_count", "created_at", "updated_at", "tier", "workspace", "archived")
    return json.dumps(
        {"source": source or "all", "sessions": [{k: r.get(k) for k in keys} for r in rows]}, indent=2
    )


def renew(
    agent: Any,
    store: Any,
    query: str = "",
    *,
    include_archived: bool = False,
    interactive: bool = False,
    source: str | None = None,
) -> str:
    """The one function behind ``/renew``, ``/resume`` and the ``--renew`` startup flag."""
    from . import renew_sources as src

    if query.strip().lower() == "confirm-tier" and source is None:
        return src.confirm_tier(agent, store)
    if source and source != src.NATIVE and source not in src.readers():
        return src.no_reader_message(source)
    rows = aggregate_rows(store, agent.session, source=source, include_archived=include_archived)
    from ..follow_ups import remember_list

    remember_list(agent, "renew", [r["id"] for r in rows])
    if not rows:
        if source and source != src.NATIVE:
            return (
                f"\n  No {source} sessions were found for you on this machine. Try {_cli_slash()} for every "
                f"source, or /sessions for the chat's own.\n" + src.explain_empty(source)
            )
        return "\n  No saved sessions to renew.\n"
    if not query.strip():
        if not interactive:
            return _listing(rows, source)
        chosen = choose_in_repl(rows)
        if chosen is None:
            return "\n  Cancelled; nothing was renewed.\n"
        return resume_session(store, agent, chosen["id"])
    found = resolve(rows, query)
    if found.problem:
        return f"\n  {found.problem}.\n"
    if found.unique is None:
        return _ambiguous(found.rows)
    return resume_session(store, agent, found.unique["id"])


def _cli_slash() -> str:
    return "/renew"


def parse_args(args: list[str]) -> tuple[str, bool]:
    request = parse_request(args)
    return request.query, request.include_archived


FOLLOW_UPS = {
    "open": FollowUp("/renew 1", "resume the most recent session"),
    "back": FollowUp("/renew {previous}", "go back to the session you were in; it was saved first"),
    "where": FollowUp("/where", "where the next message will go"),
    "context": FollowUp("/context", "what was loaded from the session"),
    "fork": FollowUp("/fork", "branch from here and leave the renewed session as it was"),
    "all": FollowUp("/renew", "every source, most recent first"),
    "new": FollowUp("/new", "start a fresh session instead"),
    "confirm": FollowUp("/renew confirm-tier", "confirm the imported content's classification so cloud providers may be used"),
}


def _with_follow_ups(ctx: CommandContext, out: str, previous: str) -> str:
    """Attach the next steps a /renew result earns, by what it was."""
    from ..follow_ups import result

    text = out if isinstance(out, str) else str(out)
    if "\n  renewed " in text[:40] or text.lstrip().startswith("renewed "):
        keys: list = ["where", "context"]
        now = getattr(getattr(ctx.agent, "session", None), "session_id", "")
        if previous and previous != now:
            keys.insert(0, ("back", {"previous": previous}))
        if "/renew confirm-tier" in text:
            keys.append("confirm")
        else:
            keys.append("fork")
        return result("/renew", text, *keys)
    if "Sessions you can renew" in text:
        return result("/renew", text, "open")
    if "No saved sessions" in text or "were found for you" in text:
        return result("/renew", text, "new")
    if "More than one session matches" in text or "no session number" in text:
        return result("/renew", text, "all")
    return text


def renew_handler(ctx: CommandContext, args: list[str]):
    previous = getattr(getattr(ctx.agent, "session", None), "session_id", "") or ""
    out = _renew_handler(ctx, args)
    return _with_follow_ups(ctx, out, previous) if isinstance(out, str) else out


def _renew_handler(ctx: CommandContext, args: list[str]):
    request = parse_request(args)
    if request.bad_source:
        from . import renew_sources as src

        return (
            f"\n  {request.bad_source!r} is not a harness name I know. Sources: "
            f"{', '.join(o.name for o in src.harness_options())}.\n"
        )
    if request.as_json:
        return list_json(
            ctx.store, ctx.agent.session, source=request.source, include_archived=request.include_archived
        )
    return renew(
        ctx.agent,
        ctx.store,
        request.query,
        include_archived=request.include_archived,
        interactive=ctx.surface == "repl" and _stdin_is_tty(),
        source=request.source,
    )


def _default_store() -> Any:
    from axiom.infra.orchestrator.session import SessionStore

    return SessionStore()


def complete(agent: Any, args_so_far: list[str]) -> list[tuple[str, str]]:
    """The harnesses to narrow to (the Axiom MCP is installed there, or a session reader exists), then recent
    sessions as ``(id, title)``. A harness with no reader is offered and says so."""
    from . import renew_sources as src

    out: list[tuple[str, str]] = []
    try:
        out = [(o.name, o.description) for o in src.harness_options()]
    except Exception:  # noqa: BLE001 - completion must never raise into the prompt
        out = []
    store = getattr(agent, "session_store", None) or _default_store()
    try:
        session = getattr(agent, "session", None)
        if session is None:  # completion with no agent (the prompt, before one exists): the local principal's view
            from axiom.infra.orchestrator.session import Session, _default_principal

            session = Session(principal_id=_default_principal())
        rows = session_rows(store, session, limit=_COMPLETE_LIMIT)
    except Exception:  # noqa: BLE001
        return out
    return out + [(r["id"], r["title"] or "(untitled)") for r in rows]


def startup_choice(store: Any, token: str, source: str | None = None) -> tuple[str, str]:
    """For ``--renew [id|text] [--harness NAME]`` before any agent exists: ``(session_id | "__pick__", "")`` or
    ``("", why)``. A foreign session comes back as ``harness:id``, which the caller imports through the same
    ``resume_session`` /renew uses."""
    from axiom.infra.orchestrator.session import Session, _default_principal

    from . import renew_sources as src

    if source and source != src.NATIVE and source not in src.readers():
        return "", src.no_reader_message(source).strip()
    if token == "__pick__":
        return "__pick__", ""
    principal = _default_principal()
    rows = aggregate_rows(store, Session(principal_id=principal), source=source)
    if not rows:
        return "", "No saved sessions to renew."
    found = resolve(rows, token)
    if found.problem:
        return "", found.problem + "."
    if found.unique is None:
        return "", _ambiguous(found.rows).strip("\n")
    return found.unique["id"], ""


def startup_import(store: Any, session_id: str) -> tuple[str, str]:
    """For ``--renew`` naming another harness's session: bring it in as a native session (the same import
    ``/renew`` does) and return ``(native_session_id, text_to_show)``; ``("", why)`` if it cannot be read."""
    from axiom.infra.orchestrator.session import Session, _default_principal

    probe = type("Probe", (), {})()
    probe.session = Session(principal_id=_default_principal())
    probe.gateway = None
    text = resume_session(store, probe, session_id)
    imported = probe.session
    if imported is None or not getattr(imported, "context", None) or "renewed_from" not in imported.context:
        return "", text.strip()
    return imported.session_id, text


USAGE = "/renew [harness|text]"
SUMMARY = (
    "Pick a previous session, from this chat or another harness, and carry on "
    "(/renew <harness> narrows; --all archived; --json; confirm-tier)"
)

# `/resume` is declared with the other session commands in builtin_commands and shares this handler.
REGISTRY.register(
    Command(
        "/renew", SUMMARY, "Session", renew_handler, usage=USAGE, complete=complete,
        follow_ups=FOLLOW_UPS,
        cli=CliSpec(
            ("--renew",), existing=True, metavar="ID_OR_TEXT",
            help="resume a previous session (bare = the picker; --harness narrows)",
        ),
    )
)
