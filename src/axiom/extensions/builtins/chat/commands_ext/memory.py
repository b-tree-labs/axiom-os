# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/memory``: search, open, list, add and forget the user's memory from inside the chat (ADR-168).

Reads ride the same door as the MCP ``axiom_memory_recall`` tool: ``MemoryServingService`` (recall -> the
fail-closed gate), with the chat's own consumer coordinate (this principal, this harness, the active
provider's deployment tier). Whatever the gate denies (vault, secret-shaped, unlabeled, another account,
owned-tier content bound for a remote model) is counted by reason and never printed. A second filter then
applies the provider tier policy (``llm.tier_policy``): controlled content is not shown to a session whose
provider is not cleared for it, and the number withheld is stated.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..command_registry import CliSpec, McpForm, command
from ..follow_ups import FollowUp, result

_SNIPPET = 200
_AGENT = "axiom-chat"
_SUBS = ("search", "show", "recent", "add", "forget")
_USAGE = (
    "  /memory search <query>       recall from your memory, through the serving gate\n"
    "  /memory show <id>            open one memory (an id prefix works)\n"
    "  /memory recent [n]           your newest memories (default 5)\n"
    "  /memory add <text>           remember a fact, labelled with this session\n"
    "  /memory forget <id> --yes    redact a memory from recall"
)


@dataclass
class _Served:
    """A gate result after the tier policy: what may be shown, and what was withheld and why."""

    items: list
    denials: Counter
    tier_withheld: int
    degraded: bool = False

    @property
    def withheld(self) -> int:
        return sum(self.denials.values()) + self.tier_withheld


# ---- the tier policy over served items -----------------------------------------------------------


def is_controlled(classification: dict | None) -> bool:
    """True when a fragment carries any classification, compartment or export-control regime."""
    c = classification or {}
    if c.get("level", "unclassified") != "unclassified" or c.get("compartments"):
        return True
    ec = c.get("export_control") or {}
    return bool(ec.get("itar") or ec.get("ear_categories") or ec.get("part_810_applicable"))


def apply_tier_policy(items: list, provider_tier: str) -> tuple[list, int]:
    """Drop controlled items a provider of ``provider_tier`` is not admitted to (``llm.tier_policy``)."""
    from axiom.llm.tier_policy import CONTROLLED, admits

    if admits(provider_tier, CONTROLLED):
        return list(items), 0
    kept = [i for i in items if not is_controlled(i.classification)]
    return kept, len(items) - len(kept)


def _finish(result, provider_tier: str) -> _Served:
    kept, withheld = apply_tier_policy(result.items, provider_tier)
    return _Served(kept, Counter(d.reason.value for d in result.denials), withheld, result.degraded)


# ---- rendering -----------------------------------------------------------------------------------


def _origin_label(comp, fragment_id: str) -> str:
    """Where a fragment came from: the harness/account it was imported from, else native."""
    from axiom.memory.fragment import fragment_from_dict

    try:
        art = comp.artifact_registry.find_by_name("fragment", fragment_id)
        origin = fragment_from_dict(art[0].data).provenance.origin if art else None
    except Exception:  # noqa: BLE001 - provenance is a label, never a reason to fail the listing
        origin = None
    return f"imported from {origin.harness} ({origin.account})" if origin else "native"


def _one_line(text: str, limit: int = _SNIPPET) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


#: Why each class of denial happens, and what (if anything) would change it. Said plainly: some cannot change.
_WHY_WITHHELD = {
    "vault": "kept in the vault by construction; nothing in chat can show it",
    "secret_routed_to_vault": "looked like a credential and was routed to the vault; it is never shown here",
    "unlabeled": "carries no classification label, so it fails closed; labelling it at its source is the fix",
    "cross_account": "belongs to another account; only sharing it from that account would show it",
    "tier_mismatch": "its deployment tier does not match the active provider; /model can switch to one that does",
    "unresolved_consumer": "this chat could not be identified to the gate; nothing can show it until it can",
    "policy_denied": "a policy denies this consumer; a policy owner can change that, a chat command cannot",
    "policy_error": "the policy check failed, so it fails closed; retry, and if it persists see /doctor",
    "policy_unavailable": "the policy source is unreachable, so it fails closed; retry once it is back",
}


def _withheld_lines(served: _Served, provider: str) -> list[str]:
    if not served.withheld:
        return []
    parts = [f"{reason} x{n}" for reason, n in sorted(served.denials.items())]
    if served.tier_withheld:
        parts.append(f"tier policy x{served.tier_withheld}")
    lines = [f"  withheld (content is never shown): {', '.join(parts)}"]
    for reason in sorted(served.denials):
        lines.append(f"    {reason}: {_WHY_WITHHELD.get(reason, 'denied by the serving gate')}")
    if served.tier_withheld:
        lines.append(
            f"  the tier policy keeps controlled content from {provider or 'the active provider'}, "
            "which is not cleared for it; switch to a cleared provider to see it"
        )
    return lines


FOLLOW_UPS = {
    "show": FollowUp("/memory show {id}", "open the first result in full"),
    "forget": FollowUp("/memory forget {id}", "redact it from recall (asks for confirmation first)"),
    "recent": FollowUp("/memory recent", "your newest memories"),
    "search": FollowUp("/memory search {query}", "search again"),
    "where": FollowUp("/where", "which provider is active; its tier decides what controlled memory it may see"),
    "model": FollowUp("/model", "switch to a provider cleared for the withheld content"),
    "add": FollowUp("/memory add {text}", "remember something new"),
}


def _withheld_keys(served: _Served) -> list[str]:
    keys: list[str] = []
    if served.tier_withheld or "tier_mismatch" in served.denials:
        keys += ["where", "model"]
    return keys


def _header(title: str, served: _Served) -> str:
    note = "  (dense retrieval unavailable; keyword results only)" if served.degraded else ""
    return f"  {title}: {len(served.items)} served, {served.withheld} withheld{note}"


def _item_lines(comp, items: list) -> list[str]:
    out: list[str] = []
    for n, item in enumerate(items, 1):
        out.append(
            f"  {n}. {item.fragment_id}  [{item.cognitive_type}]  {_origin_label(comp, item.fragment_id)}"
        )
        out.append(f"       {_one_line(item.text)}")
    return out


# ---- the context a subcommand runs in -------------------------------------------------------------


@dataclass
class _Mem:
    agent: object
    principal: str
    service: object
    consumer: object
    provider: str
    provider_tier: str

    @property
    def comp(self):
        return self.service.composition


def _open(agent) -> _Mem | str:
    """The serving context for this chat, or the sentence that says why there is none."""
    from axiom.memory.pg_store import MemoryStoreUnavailable

    from .. import memory_wiring as mw
    from ..where import where_report

    try:
        principal = mw.session_principal(agent)
    except ValueError as exc:
        return f"\n  No principal to read memory for: {exc}\n"
    try:
        service = mw.serving_service_for(agent)
    except MemoryStoreUnavailable as exc:
        return f"\n  Memory is unavailable: {exc}\n"
    report = where_report(agent)
    return _Mem(
        agent, principal, service, mw.consumer_for(agent, principal), report.provider, report.tier
    )


def _mine(m: _Mem) -> list:
    return [
        a
        for a in m.comp.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("provenance", {}).get("principal_id") == m.principal
    ]


def _lookup(m: _Mem, ident: str) -> tuple[str | None, str | None]:
    """Exact id, else a unique prefix among this principal's fragments: ``(id, None)`` or ``(None, sentence)``."""
    if m.comp.artifact_registry.find_by_name("fragment", ident):
        return ident, None
    hits = sorted(a.name for a in _mine(m) if a.name.startswith(ident))
    if not hits:
        return None, f"\n  No memory with id {ident!r} for {m.principal}.\n"
    if len(hits) > 1:
        listed = "\n".join(f"    {h}" for h in hits[:8])
        return None, f"\n  {ident!r} matches {len(hits)} memories; use more of the id:\n{listed}\n"
    return hits[0], None


# ---- subcommands ---------------------------------------------------------------------------------


def _out(text: str, keys: list):
    return result("/memory", text, *keys)


def _search_served(m: _Mem, query: str, k: int = 5) -> _Served:
    result = m.service.serve(
        query, consumer=m.consumer, recall_user=m.principal, recall_agent=_AGENT, k=k
    )
    return _finish(result, m.provider_tier)


def _search(m: _Mem, args: list[str]) -> str:
    if not args:
        return "\n  Usage: /memory search <query>\n"
    query = " ".join(args)
    try:
        served = _search_served(m, query)
    except RuntimeError as exc:  # a composition with no recall index
        return f"\n  Recall is unavailable: {exc}\n"
    lines = [_header(f"Memory search {query!r}", served), *_item_lines(m.comp, served.items)]
    lines += _withheld_lines(served, m.provider)
    keys: list = [("show", {"id": served.items[0].fragment_id})] if served.items else ["recent"]
    return _out("\n" + "\n".join(lines) + "\n", keys + _withheld_keys(served))


def _show(m: _Mem, args: list[str]) -> str:
    if not args:
        return "\n  Usage: /memory show <id>\n"
    fid, err = _lookup(m, args[0])
    if err or fid is None:
        return err or ""
    frags = m.comp.read([fid], user=m.principal, agent=_AGENT)
    if not frags:
        return f"\n  Memory {fid} exists but you do not have access to read it.\n"
    result = m.service.serve_fragments(
        frags, consumer=m.consumer, query=f"/memory show {fid}", agent=_AGENT
    )
    served = _finish(result, m.provider_tier)
    if not served.items:
        lines = [f"  Memory {fid} was not served.", *_withheld_lines(served, m.provider)]
        return _out("\n" + "\n".join(lines) + "\n", _withheld_keys(served) or ["recent"])
    frag = frags[0]
    lines = [
        f"  {fid}  [{frag.cognitive_type.value}]",
        f"  written {frag.provenance.timestamp} by {frag.provenance.principal_id}"
        + (f", session {frag.provenance.session_id}" if frag.provenance.session_id else ""),
        f"  origin: {_origin_label(m.comp, fid)}",
        "",
        *[f"  {ln}" for ln in served.items[0].text.strip().splitlines()],
    ]
    return _out("\n" + "\n".join(lines) + "\n", [("forget", {"id": fid}), "recent"])


def _recent_served(m: _Mem, n: int) -> tuple[_Served, int]:
    from axiom.memory.fragment import fragment_from_dict

    n = max(1, min(n, 50))
    newest = sorted(_mine(m), key=lambda a: a.data["provenance"].get("timestamp", ""), reverse=True)
    kept: list = []
    denials: Counter = Counter()
    tier_withheld = 0
    examined = 0
    for art in newest:
        if len(kept) >= n:
            break
        try:
            frag = fragment_from_dict(art.data)
        except Exception:  # noqa: BLE001 - an undecodable row is not a memory
            continue
        examined += 1
        result = m.service.serve_fragments(
            [frag], consumer=m.consumer, query="/memory recent", agent=_AGENT
        )
        one = _finish(result, m.provider_tier)
        kept += one.items
        denials.update(one.denials)
        tier_withheld += one.tier_withheld
    return _Served(kept, denials, tier_withheld), examined


def _recent(m: _Mem, args: list[str]) -> str:
    try:
        n = int(args[0]) if args else 5
    except ValueError:
        return "\n  Usage: /memory recent [n]\n"
    served, examined = _recent_served(m, n)
    kept = served.items
    title = f"Recent memory ({examined} newest examined)"
    lines = [_header(title, served), *_item_lines(m.comp, kept), *_withheld_lines(served, m.provider)]
    keys: list = [("show", {"id": kept[0].fragment_id})] if kept else [("add", {"text": "<text>"})]
    return _out("\n" + "\n".join(lines) + "\n", keys + _withheld_keys(served))


def _add(m: _Mem, text: str) -> str:
    from axiom.memory.serving import looks_like_secret

    if not text:
        return "\n  Usage: /memory add <text>\n"
    if looks_like_secret(text):
        return (
            "\n  Not remembered: that reads as a credential, and memory is never where credentials "
            "live. Keep it in the vault.\n"
        )
    frag = m.comp.write(
        content={"summary": text, "fact_kind": "user_stated", "source": "chat:/memory add"},
        cognitive_type="semantic",
        principal_id=m.principal,
        agents={_AGENT},
        resources=set(),
        accountable_human_id=m.principal,
        session_id=getattr(m.agent.session, "session_id", "") or "",
    )
    return _out(
        f"\n  Added {frag.id} (session {frag.provenance.session_id or 'none'}).\n",
        [("show", {"id": frag.id}), ("forget", {"id": frag.id})],
    )


def _forget(m: _Mem, args: list[str]) -> str:
    ids = [a for a in args if a != "--yes"]
    if not ids:
        return "\n  Usage: /memory forget <id> --yes\n"
    fid, err = _lookup(m, ids[0])
    if err or fid is None:
        return err or ""
    if "--yes" not in args:
        return (
            f"\n  This redacts {fid} from recall (the row is kept for audit). "
            f"Run `/memory forget {ids[0]} --yes` to confirm.\n"
        )
    res = m.comp.forget([fid], requester=m.principal, agent=_AGENT, reason="chat /memory forget")
    if res.forgotten:
        return _out(
            f"\n  Forgot {fid}; it no longer appears in recall (the row is kept for audit).\n", ["recent"]
        )
    if res.denied:
        return f"\n  Not forgotten: {m.principal} does not control {fid}.\n"
    return f"\n  No memory with id {fid!r}.\n"



# ---- data forms: the same reads as JSON-serializable data (the axiom_chat__memory_* MCP tools) ---------


def _data_item(m: _Mem, item) -> dict:
    """One served item. A second, independent secret check runs here: the gate already denies
    credential-shaped fragments, and nothing that still reads like one leaves through this door."""
    from axiom.memory.serving import looks_like_secret

    text = item.text or ""
    if looks_like_secret(text):
        text = "[withheld: reads like a credential]"
    return {
        "id": item.fragment_id,
        "type": item.cognitive_type,
        "origin": _origin_label(m.comp, item.fragment_id),
        "text": text,
    }


def _served_data(m: _Mem, served: _Served, **extra) -> dict:
    return {
        "items": [_data_item(m, i) for i in served.items],
        "served": len(served.items),
        "withheld": {
            "total": served.withheld,
            "by_reason": dict(sorted(served.denials.items())),
            "tier_policy": served.tier_withheld,
            "why": {r: _WHY_WITHHELD.get(r, "denied by the serving gate") for r in sorted(served.denials)},
        },
        "degraded": served.degraded,
        **extra,
    }


def _mem_for(agent):
    opened = _open(agent)
    if isinstance(opened, str):
        return None, {"error": opened.strip()}
    return opened, None


def memory_search_data(agent, query: str, k: int = 5) -> dict:
    m, err = _mem_for(agent)
    if err:
        return err
    if not query.strip():
        return {"error": "a query is required"}
    try:
        return _served_data(m, _search_served(m, query, max(1, min(int(k or 5), 20))), query=query)
    except RuntimeError as exc:
        return {"error": f"Recall is unavailable: {exc}"}


def memory_recent_data(agent, n: int = 5) -> dict:
    m, err = _mem_for(agent)
    if err:
        return err
    served, examined = _recent_served(m, int(n or 5))
    return _served_data(m, served, examined=examined)


def memory_show_data(agent, ident: str) -> dict:
    m, err = _mem_for(agent)
    if err:
        return err
    fid, why = _lookup(m, ident)
    if why or fid is None:
        return {"error": (why or "").strip()}
    frags = m.comp.read([fid], user=m.principal, agent=_AGENT)
    if not frags:
        return {"error": f"Memory {fid} exists but you do not have access to read it."}
    result = m.service.serve_fragments(frags, consumer=m.consumer, query=f"/memory show {fid}", agent=_AGENT)
    return _served_data(m, _finish(result, m.provider_tier), id=fid)


# ---- the command ---------------------------------------------------------------------------------


@command(
    "/memory",
    "Search, open, list, add and forget your memory (served through the gate)",
    "Memory & knowledge",
    usage="/memory <search|show|recent|add|forget>",
    subcommands={
        "search": "Recall from your memory: /memory search <query>",
        "show": "Open one memory: /memory show <id>",
        "recent": "Your newest memories: /memory recent [n]",
        "add": "Remember a fact, labelled with this session: /memory add <text>",
        "forget": "Redact a memory from recall: /memory forget <id> --yes",
    },
    mutates=True,
    cli=CliSpec(("--no-memory",), help="start without long-term memory: nothing is recalled or recorded"),
    risk="destructive",  # /memory forget redacts; it is never an MCP form (the read forms below are)
    follow_ups=FOLLOW_UPS,
    expose=frozenset({"chat", "cli", "mcp"}),
    mcp=(
        McpForm("chat.memory_search", "/memory search"),
        McpForm("chat.memory_show", "/memory show"),
        McpForm("chat.memory_recent", "/memory recent"),
    ),
)
def memory_command(ctx, args: list[str]) -> str:
    if not args or args[0] not in _SUBS:
        return f"\n{_USAGE}\n"
    opened = _open(ctx.agent)
    if isinstance(opened, str):
        return opened
    sub, rest = args[0], args[1:]
    if sub == "add":
        parts = (ctx.line or "").split(None, 2)
        return _add(opened, parts[2].strip() if len(parts) > 2 else "")
    return {"search": _search, "show": _show, "recent": _recent, "forget": _forget}[sub](opened, rest)
