# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Always-on platform-primitive MCP tools.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §8.

These are the seven tools any Axiom node exposes regardless of which
extensions are installed. Phase-2 wires six of them to real services
(``CompositionService``, ``NodeRegistry``, ``BriefingService``,
``HookRegistry``); the seventh — ``axiom_rag__retrieve`` — stays a
fail-soft stub while the parallel RAG-rebuild session is in flight,
per the Phase-2 task brief.

Per-call principal resolution pulls the local ``@name:context`` identity
from :func:`axiom.vega.federation.identity.load_identity` (see the
chat-extension precedent in ``chat/memory_wiring.py``); when no identity
is configured the tools fall back to ``@unknown:local`` so a fresh node
can still write its own bootstrap fragments.

Per ``feedback_axiom_domain_agnostic`` no description references any
specific consumer domain.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.types import Tool

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inventory — pin the seven tools the spec promises (§8 table)
# ---------------------------------------------------------------------------

PLATFORM_TOOL_NAMES: tuple[str, ...] = (
    "axiom_memory__compose",
    "axiom_memory__retrieve",
    "axiom_memory__list",
    "axiom_federation__node_status",
    "axiom_rag__retrieve",
    "axiom_signals__brief",
    "axiom_node__hooks_list",
)


# ---------------------------------------------------------------------------
# Per-call principal resolution (spec §8 — defaults to local node identity)
# ---------------------------------------------------------------------------


def _resolve_principal(args: dict[str, Any]) -> str:
    """Resolve the calling principal: explicit arg > local identity > anon.

    Returns a Matrix-style ``@name:context`` per
    ``feedback_principal_naming``. The chat-extension's ``memory_wiring``
    is the precedent: when an identity exists locally we prefer the
    configured ``display_name`` (already shaped ``name:context``), prefix
    it with ``@`` if the user omitted it, and fall back to
    ``@unknown:local`` only when neither is available.
    """
    explicit = (args.get("principal") or "").strip()
    if explicit:
        return explicit if explicit.startswith("@") else f"@{explicit}"
    try:
        from axiom.vega.federation.identity import load_identity

        identity = load_identity()
    except Exception:
        identity = None
    if identity is not None:
        display = (identity.display_name or "").strip()
        if display:
            return display if display.startswith("@") else f"@{display}"
    return "@unknown:local"


# ---------------------------------------------------------------------------
# Lazily-built shared CompositionService (one per process, cached)
# ---------------------------------------------------------------------------


_COMPOSITION_CACHE: Any = None


def _axiom_home() -> Path:
    env = os.environ.get("AXIOM_HOME")
    if env:
        return Path(env)
    return Path(os.environ.get("HOME", ".")).expanduser() / ".axiom"


def _build_composition() -> Any:
    """Build (or reuse) a CompositionService rooted at the user state dir.

    Mirrors ``chat.memory_wiring._build_user_composition`` so MCP writes
    and ``axi memory show`` reads share one ArtifactRegistry. Cached
    per-process — the platform-primitive surface is loaded once at
    server boot, so caching here matches that lifecycle.
    """
    global _COMPOSITION_CACHE
    if _COMPOSITION_CACHE is not None:
        return _COMPOSITION_CACHE

    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.infra.paths import get_user_state_dir
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import Keypair, generate_keypair

    base = get_user_state_dir() / "memory"
    base.mkdir(parents=True, exist_ok=True)
    key_path = base / "node.key"
    if key_path.exists():
        kp = Keypair.from_private_bytes(key_path.read_bytes())
    else:
        kp = generate_keypair()
        key_path.write_bytes(kp.export_private())

    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    _COMPOSITION_CACHE = CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )
    return _COMPOSITION_CACHE


def _reset_composition_cache() -> None:
    """Test-only: drop the cached service so tmp_axiom_home gets a fresh DB."""
    global _COMPOSITION_CACHE
    _COMPOSITION_CACHE = None


# ---------------------------------------------------------------------------
# memory: compose / retrieve / list
# ---------------------------------------------------------------------------


async def _memory_compose(args: dict[str, Any]) -> dict[str, Any]:
    """Write a memory fragment via CompositionService (spec §8 row 1)."""
    from datetime import datetime, timezone

    # Test isolation: each test gets a fresh tmp_axiom_home, so we drop
    # the per-process cache when AXIOM_HOME has changed since last build.
    composition = _composition_for_call()

    principal = _resolve_principal(args)
    accountable = (
        args.get("accountable_human_id")
        or args.get("accountable_human")
        or principal
    )
    content = dict(args.get("content") or {})
    if not isinstance(content, dict):
        return {"error": "argument 'content' must be a JSON object"}
    cognitive_type = (args.get("kind") or args.get("cognitive_type") or "episodic").strip()

    # Episodic fragments must carry ``event_time`` in their content per
    # the MemoryFragment contract. Auto-fill with "now" when the caller
    # omits it — the MCP surface is meant to be ergonomic, not a literal
    # passthrough of the underlying field discipline.
    if cognitive_type == "episodic" and "event_time" not in content:
        content["event_time"] = datetime.now(timezone.utc).isoformat()

    # Honor caller-supplied agents + resources so the (T, U, A, R)
    # provenance audit reflects the actual writer (e.g. claude-opus-4-7
    # via claude-code, or gpt-5-5 via codex), NOT the MCP transport
    # itself. Always append "mcp_root_server" so cross-vendor audits can
    # still distinguish "wrote via the MCP gateway" from direct
    # CompositionService calls — but it's an addition, not a replacement.
    def _as_str_set(value: Any) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, (list, tuple, set)):
            return {str(v) for v in value if v}
        if isinstance(value, str):
            return {value} if value else set()
        return {str(value)}

    caller_agents = _as_str_set(args.get("agents"))
    caller_agents.add("mcp_root_server")
    caller_resources = _as_str_set(args.get("resources"))

    # Honor caller-supplied session_id so fragments carry the writer's
    # session, not the MCP server process's. When omitted, the write
    # path's default resolution kicks in (empty in test/sentinel contexts;
    # the MCP server's current session otherwise). Spec-memory §3.7.
    caller_session_id = args.get("session_id")
    if isinstance(caller_session_id, str):
        caller_session_id = caller_session_id.strip()
    else:
        caller_session_id = None

    try:
        fragment = composition.write(
            content=content,
            cognitive_type=cognitive_type,
            principal_id=principal,
            agents=caller_agents,
            resources=caller_resources,
            accountable_human_id=accountable,
            session_id=caller_session_id,
        )
    except Exception as exc:  # noqa: BLE001 — translate every error
        return {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "fragment_id": fragment.id,
        "kind": fragment.cognitive_type.value,
        "principal": principal,
        "timestamp": fragment.provenance.timestamp,
    }


async def _memory_retrieve(args: dict[str, Any]) -> dict[str, Any]:
    """Read fragments by filter (spec §8 row 2).

    Default response now includes the full ``content`` blob so callers
    don't have to issue a second per-fragment fetch — Phase-2 truncation
    to summary-only forced clients to ``axiom_memory__compose``-then-
    re-list patterns that lost the rich data we just wrote.

    Set ``include_content=False`` to opt back into the lean shape (id +
    cognitive_type + timestamp + principal + fact_kind + summary only)
    when paging through many fragments where size matters.
    """
    composition = _composition_for_call()

    try:
        limit = int(args.get("limit", 10))
    except (TypeError, ValueError):
        return {"error": "argument 'limit' must be an integer"}

    principal_filter = (args.get("principal") or "").strip() or None
    cohort_filter = (args.get("cohort") or "").strip() or None
    kind_filter = (args.get("kind") or "").strip() or None
    include_content = args.get("include_content", True)
    if isinstance(include_content, str):
        include_content = include_content.strip().lower() not in ("false", "0", "no", "")

    # Spec-memory §3.7.3 scope rules. ``session_id`` is the caller's
    # current session (e.g. Claude Code's session, not the MCP server's).
    # ``scope`` selects the filtering policy:
    #   - "default"            type-aware (episodic→strict, others→cross)
    #   - "strict" / "current" all types filtered to session_id
    #   - "all"                no session filter
    #   - "session:<id>"       filter to the given session_id
    scope_raw = (args.get("scope") or "default").strip()
    caller_session_id = (args.get("session_id") or "").strip()
    explicit_session: str | None = None
    # Lowercase only the keyword prefix — the URI tail keeps its casing
    # so ``session:session://Abc`` resolves to ``session://Abc``, not
    # the silently-mangled lowercase form.
    if scope_raw.lower().startswith("session:"):
        explicit_session = scope_raw.split(":", 1)[1].strip() or None
        scope_arg = "explicit"
    else:
        scope_arg = scope_raw.lower()

    def _passes_session_filter(prov_session: str, cognitive_type: str) -> bool:
        # Empty (legacy / pre-session) fragments are interpreted as
        # cross-session — spec §3.7.3 "Backwards compatibility".
        if prov_session == "":
            return True
        if scope_arg == "all":
            return True
        if scope_arg == "explicit":
            return prov_session == explicit_session
        if scope_arg in ("strict", "current"):
            return prov_session == caller_session_id if caller_session_id else True
        # "default" — MIRIX-type-aware
        if cognitive_type == "episodic":
            return prov_session == caller_session_id if caller_session_id else True
        # core / procedural / resource / semantic cross sessions by default
        return True

    artifacts = composition.artifact_registry.list(kind="fragment") or []

    # Sort newest-first so `limit=N` returns the N most recent fragments,
    # which is the ergonomic default every chat client wants. Without this
    # sort, list() returns insertion order ascending and `limit=5` shows
    # the OLDEST 5 — exactly the opposite of what the caller meant.
    def _ts(artifact) -> str:
        return ((artifact.data or {}).get("provenance") or {}).get("timestamp", "")

    artifacts = sorted(artifacts, key=_ts, reverse=True)

    fragments: list[dict[str, Any]] = []
    for artifact in artifacts:
        data = artifact.data or {}
        prov = data.get("provenance") or {}
        content = data.get("content") or {}
        cognitive_type = data.get("cognitive_type", "")
        if principal_filter and prov.get("principal_id") != principal_filter:
            continue
        if kind_filter and cognitive_type != kind_filter:
            continue
        if cohort_filter:
            if (
                content.get("cohort") != cohort_filter
                and content.get("scope") != cohort_filter
            ):
                continue
        if not _passes_session_filter(prov.get("session_id", ""), cognitive_type):
            continue
        entry: dict[str, Any] = {
            "id": data.get("id", artifact.name),
            "cognitive_type": cognitive_type,
            "timestamp": prov.get("timestamp", ""),
            "principal": prov.get("principal_id", ""),
            "fact_kind": content.get("fact_kind", ""),
            "summary": content.get("summary", ""),
        }
        if include_content:
            entry["content"] = content
            # Provenance is the (T, U, A, R, S) audit tuple — useful for
            # any client doing cross-vendor verification work or
            # cross-session attribution per spec-memory §3.7.3.
            entry["provenance"] = {
                "agents": prov.get("agents", []),
                "resources": prov.get("resources", []),
                "principal_id": prov.get("principal_id", ""),
                "timestamp": prov.get("timestamp", ""),
                "session_id": prov.get("session_id", ""),
            }
        fragments.append(entry)
        if len(fragments) >= limit:
            break

    return {
        "fragments": fragments,
        "limit": limit,
        "principal": principal_filter,
        "cohort": cohort_filter,
        "kind": kind_filter,
        "include_content": include_content,
        "scope": scope_arg if scope_arg != "explicit"
            else f"session:{explicit_session}",
        "session_id": caller_session_id or None,
    }


async def _memory_list(args: dict[str, Any]) -> dict[str, Any]:
    """Enumerate principals + scopes the node has memory for (spec §8 row 3)."""
    composition = _composition_for_call()
    artifacts = composition.artifact_registry.list(kind="fragment") or []
    principals: set[str] = set()
    scopes: set[str] = set()
    for artifact in artifacts:
        data = artifact.data or {}
        prov = data.get("provenance") or {}
        pid = prov.get("principal_id") or ""
        if pid:
            principals.add(pid)
        content = data.get("content") or {}
        scope = content.get("scope") or content.get("cohort") or ""
        if scope:
            scopes.add(scope)
    return {
        "principals": sorted(principals),
        "scopes": sorted(scopes),
        "fragment_count": len(artifacts),
    }


_LAST_AXIOM_HOME: str | None = None


def _composition_for_call() -> Any:
    """Resolve the active composition, busting cache if AXIOM_HOME changed.

    Test fixtures monkeypatch ``AXIOM_HOME`` per-test; without this guard
    tests share state through the module-level cache and a write under
    one tmp_axiom_home would leak into the next test's view.
    """
    global _LAST_AXIOM_HOME
    current = os.environ.get("AXIOM_HOME") or os.environ.get("HOME") or ""
    if current != _LAST_AXIOM_HOME:
        _reset_composition_cache()
        _LAST_AXIOM_HOME = current
    return _build_composition()


# ---------------------------------------------------------------------------
# federation: node_status — backed by NodeRegistry
# ---------------------------------------------------------------------------


async def _federation_node_status(args: dict[str, Any]) -> dict[str, Any]:
    """Return real federation peer/trust status from the local NodeRegistry."""
    home = _axiom_home()
    cohorts_dir = home / "federation" / "cohorts"
    cohorts: list[str] = []
    if cohorts_dir.is_dir():
        cohorts = sorted(p.name for p in cohorts_dir.iterdir() if p.is_dir())

    peers: list[dict[str, Any]] = []
    identity_summary: dict[str, Any] = {}
    try:
        from axiom.vega.federation.discovery import NodeRegistry
        from axiom.vega.federation.identity import load_identity

        registry = NodeRegistry()
        try:
            registry.load()
        except Exception:
            pass
        for node in registry.list_all() or []:
            try:
                peers.append(node.to_dict())
            except Exception:
                peers.append({"node_id": getattr(node, "node_id", "?")})
        identity = load_identity()
        if identity is not None:
            identity_summary = {
                "node_id": identity.node_id,
                "display_name": identity.display_name,
                "owner": identity.owner,
                "profile": identity.profile,
            }
    except Exception as exc:  # noqa: BLE001 — federation optional on bare nodes
        log.debug("mcp federation.node_status: %s", exc)

    return {
        "node": str(home),
        "identity": identity_summary,
        "cohorts": cohorts,
        "peers": peers,
        "peer_count": len(peers),
    }


# ---------------------------------------------------------------------------
# rag: retrieve — config-resolved hybrid retrieval over the node's RAG store
# ---------------------------------------------------------------------------


def _resolve_rag_store() -> Any:
    """Resolve a connected RAG store from config; ``None`` when unconfigured.

    Resolution order mirrors the ``axi rag`` CLI: ``DATABASE_URL`` env, then
    the ``rag.database_url`` setting. Domain-agnostic — the *consumer* (e.g. a
    domain extension) points this at whichever pgvector/sqlite store holds its
    corpus; this primitive never names a host or endpoint.
    """
    url = os.environ.get("DATABASE_URL") or ""
    if not url:
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            url = SettingsStore().get("rag.database_url", "") or ""
        except Exception:  # noqa: BLE001 — settings absent → unconfigured
            url = ""
    if not url:
        return None

    from axiom.rag.store_factory import create_store

    store = create_store(url)
    connect = getattr(store, "connect", None)
    if callable(connect):
        connect()
    return store


def _embed_query(text: str) -> list[float] | None:
    """Embed the query with the best configured provider; ``None`` to fall
    back to keyword/FTS-only retrieval. Never raises — a configured-but-failing
    embedder degrades to text-only rather than failing the whole call.
    """
    try:
        from axiom.rag.embeddings import embed_texts

        vectors = embed_texts([text])
    except Exception:  # noqa: BLE001 — embedder optional; degrade to text-only
        return None
    if not vectors:
        return None
    return vectors[0]


async def _rag_retrieve(args: dict[str, Any]) -> dict[str, Any]:
    """Hybrid RAG retrieval (vector + keyword, RRF-fused) over the node's
    configured corpus. Spec §8.

    Fail-soft contract: an unconfigured store, a missing embedder, or a backend
    error all return a structured payload (``ok: False`` + ``note``/``error``)
    so an MCP client never sees a hard crash.
    """
    query = (args.get("query") or "").strip()
    k = int(args.get("k", 5))
    corpora = args.get("corpora") or None
    if not query:
        return {
            "query": "",
            "k": k,
            "results": [],
            "ok": False,
            "error": "argument 'query' is required and must be non-empty",
        }

    try:
        store = _resolve_rag_store()
    except Exception as exc:  # noqa: BLE001 — translate
        return {
            "query": query,
            "k": k,
            "results": [],
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if store is None:
        return {
            "query": query,
            "k": k,
            "results": [],
            "ok": False,
            "note": (
                "No RAG store configured. Set DATABASE_URL or "
                '`axi settings set rag.database_url "postgresql://..."`.'
            ),
        }

    # Stores that run hybrid search server-side (e.g. a remote peer) take a
    # single text query — embedding locally would force a wasteful second
    # round-trip and double the network-failure surface.
    embedding = None if getattr(store, "does_own_hybrid", False) else _embed_query(query)
    try:
        from axiom.rag.retriever import retrieve

        chunks = retrieve(
            store,
            query_text=query,
            query_embedding=embedding,
            corpora=corpora,
            limit=k,
        )
    except Exception as exc:  # noqa: BLE001 — translate every backend error
        return {
            "query": query,
            "k": k,
            "results": [],
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    results = [
        {
            "citation_key": c.citation_key,
            "rank": c.rank,
            "source_path": c.source_path,
            "source_title": c.source_title,
            "text": c.chunk_text,
            "corpus": c.corpus,
            "similarity": c.similarity,
            "score": c.rrf_score,
        }
        for c in chunks
    ]
    return {
        "query": query,
        "k": k,
        "mode": "hybrid" if embedding is not None else "text",
        "results": results,
        "count": len(results),
        "ok": True,
    }


# ---------------------------------------------------------------------------
# signals: brief — backed by BriefingService
# ---------------------------------------------------------------------------


async def _signals_brief(args: dict[str, Any]) -> dict[str, Any]:
    """Run the signal-briefing pipeline; return the latest brief."""
    try:
        from axiom.extensions.builtins.signals.briefing import (
            get_briefing_service,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "topics": [],
            "brief": "",
            "ok": False,
            "error": f"signals.briefing unavailable: {exc}",
        }

    try:
        service = get_briefing_service()
        briefing = service.brief_me(
            since=args.get("since"),
            topic=args.get("topic"),
            acknowledge=False,
        )
    except Exception as exc:  # noqa: BLE001 — translate
        return {
            "topics": [],
            "brief": "",
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    payload = briefing.to_dict()
    payload["ok"] = True
    return payload


# ---------------------------------------------------------------------------
# node: hooks_list — backed by HookRegistry discovery
# ---------------------------------------------------------------------------


async def _node_hooks_list(args: dict[str, Any]) -> dict[str, Any]:
    """List manifest-declared hooks installed on this node (diagnostics)."""
    try:
        from axiom.extensions.discovery import discover_extensions
        from axiom.infra.hooks.registry import discover_manifest_hooks
    except Exception as exc:  # noqa: BLE001
        return {"hooks": [], "ok": False, "error": str(exc)}

    try:
        extensions = list(discover_extensions())
    except Exception as exc:  # noqa: BLE001
        return {"hooks": [], "ok": False, "error": f"discovery failed: {exc}"}

    interceptors, observers = discover_manifest_hooks(extensions)

    hook_entries: list[dict[str, Any]] = []
    for spec in interceptors:
        hook_entries.append(
            {
                "kind": "interceptor",
                "event": spec.event,
                "source": spec.source,
                "priority": spec.priority,
                "fail_mode": str(spec.fail_mode),
            }
        )
    for event, _pattern, _fn, fail_mode, source in observers:
        hook_entries.append(
            {
                "kind": "observer",
                "event": event,
                "source": source,
                "fail_mode": str(fail_mode),
            }
        )
    return {
        "hooks": hook_entries,
        "interceptor_count": len(interceptors),
        "observer_count": len(observers),
        "extension_count": len(extensions),
    }


# ---------------------------------------------------------------------------
# Public surface — tool metadata + dispatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PlatformToolDef:
    name: str
    description: str
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    input_schema: dict[str, Any]


def _input_schema_for(name: str) -> dict[str, Any]:
    """Return a permissive JSON-Schema for a platform tool.

    Strict per-tool schemas land once we're sure the underlying service
    signatures are stable. The current schemas accept any object —
    clients see the tool exists, can call it, and the handler tolerates
    missing args via defaults.
    """
    base: dict[str, Any] = {"type": "object", "additionalProperties": True}
    if name == "axiom_memory__compose":
        base["properties"] = {
            "kind": {"type": "string"},
            "content": {"type": "object"},
            "principal": {"type": "string"},
            "accountable_human_id": {"type": "string"},
            "provenance": {"type": "object"},
            "agents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Agent identifiers the (T,U,A,R) provenance tuple should "
                    "carry — e.g. ['claude-opus-4-7', 'claude-code-cli'] or "
                    "['gpt-5-5', 'codex-cli', 'ut-portkey-gateway']. "
                    "'mcp_root_server' is always added by the gateway."
                ),
            },
            "resources": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Resource tags for the (T,U,A,R) provenance tuple — "
                    "free-form strings naming the session, demo run, "
                    "project context, etc."
                ),
            },
        }
    elif name == "axiom_memory__retrieve":
        base["properties"] = {
            "kind": {"type": "string"},
            "principal": {"type": "string"},
            "cohort": {"type": "string"},
            "limit": {"type": "integer"},
            "include_content": {
                "type": "boolean",
                "description": "When true (default), returns the full content blob + (T,U,A,R) provenance per fragment. Set false for a lean list view when paging through many fragments.",
            },
        }
    elif name == "axiom_rag__retrieve":
        base["properties"] = {
            "query": {"type": "string"},
            "k": {"type": "integer"},
            "mode": {"type": "string"},
        }
        base["required"] = ["query"]
    elif name == "axiom_signals__brief":
        base["properties"] = {
            "since": {"type": "string"},
            "topic": {"type": "string"},
            "kinds": {"type": "array", "items": {"type": "string"}},
        }
    return base


_DESCRIPTIONS: dict[str, str] = {
    "axiom_memory__compose": (
        "Write a memory fragment through CompositionService. The canonical "
        "(T, U, A, R) provenance is enforced; ownership defaults to the calling principal."
    ),
    "axiom_memory__retrieve": (
        "Read memory fragments by filter (kind, principal, cohort, limit)."
    ),
    "axiom_memory__list": (
        "Enumerate principals and scopes for which the node has memory."
    ),
    "axiom_federation__node_status": (
        "Return this node's federation status (cohorts, peer count, trust state)."
    ),
    "axiom_rag__retrieve": (
        "Hybrid retrieval (vector + graph) over indexed sources for a query."
    ),
    "axiom_signals__brief": (
        "Run the signals briefing pipeline and return the latest brief."
    ),
    "axiom_node__hooks_list": (
        "List hooks installed on this node (diagnostics)."
    ),
}


_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "axiom_memory__compose": _memory_compose,
    "axiom_memory__retrieve": _memory_retrieve,
    "axiom_memory__list": _memory_list,
    "axiom_federation__node_status": _federation_node_status,
    "axiom_rag__retrieve": _rag_retrieve,
    "axiom_signals__brief": _signals_brief,
    "axiom_node__hooks_list": _node_hooks_list,
}


@dataclass(frozen=True)
class PlatformContribution:
    """The set of (tool, handler) pairs always merged before extensions."""

    tools: list[Tool]
    dispatch: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]


class PlatformPrimitives:
    """Static accessor — yields the always-on contribution."""

    @staticmethod
    def contribution() -> PlatformContribution:
        tools: list[Tool] = []
        for name in PLATFORM_TOOL_NAMES:
            tools.append(
                Tool(
                    name=name,
                    description=_DESCRIPTIONS[name],
                    inputSchema=_input_schema_for(name),
                )
            )
        return PlatformContribution(tools=tools, dispatch=dict(_HANDLERS))


__all__ = [
    "PLATFORM_TOOL_NAMES",
    "PlatformContribution",
    "PlatformPrimitives",
]
