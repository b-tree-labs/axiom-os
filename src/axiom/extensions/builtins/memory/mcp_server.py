# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axiom-memory MCP server — cross-tool memory ledger over stdio.

Exposes the per-principal memory ledger to any MCP-aware client (Claude
Code, ChatGPT via connectors, Gemini, OpenCode, future tools). Read tools
recall prior conversation turns; write tools log new ones.

Reads and writes both flow through the same backend that ``axi memory show``
and ``axi memory record`` use — ``CompositionService`` on the SQLite-backed
``ArtifactRegistry`` rooted in the user state directory. PyPI install and
editable repo install behave identically.

Tools:

  axiom_memory_append    — record a single conversation turn (write)
  axiom_memory_show      — list a principal's recent fragments (read)
  axiom_memory_recent    — N most-recent turns for a principal (read)
  axiom_memory_search    — filter by tool / fact_kind / substring (read)
  axiom_memory_recall    — semantic recall through the fail-closed gate (read)
  axiom_actions_recent   — N most-recent agent action-provenance records (read)
  axiom_actions_search   — filtered search over the action ledger (read)
  axiom_secrets_list     — foreign-credential names + metadata (read; issue #667)
  axiom_vault_audit      — credential-lifecycle expiry audit (read; issue #667)
  axiom_secrets_rotate_trigger — guarded KEEP rotation; returns a journaled
                                 action handle, NEVER a secret value

The secrets/vault tools live on this server the same way the #665 action
tools do — in practice only axiom-memory is registered everywhere, so new
surfaces accrete here until the single composed axiom MCP server (#669)
absorbs them. Invariant (issue #667): secret VALUES never transit MCP;
handles and metadata only, and the rotation exchange itself is
process-internal to the deterministic skill.

The server's `instructions` field is the primary mechanism driving model
discipline on writes. It is set during MCP initialization and shown to the
model so it knows to call ``axiom_memory_append`` after substantive turns.

Invocation:

    {
      "mcpServers": {
        "axiom-memory": {
          "command": "python",
          "args": ["-m", "axiom.extensions.builtins.memory.mcp_server"]
        }
      }
    }

No auth in this MVP — stdio local-only; identity comes from the local
state directory's principal binding. Cross-host access goes through the
federation gateway (Stage 5 of ADR-033, post-Prague).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

# The `mcp` SDK is an *optional* extra, not a core dependency — a plain
# ``pip install axiom-os-lm`` gives you the ledger CLI (`axi memory`) but not
# the MCP transport. Turn the otherwise-cryptic ``No module named 'mcp'`` into
# an actionable install hint so a colleague onboarding the MCP server knows
# exactly what to run.
_MCP_MISSING_HINT = (
    "The axiom-memory MCP server requires the optional 'mcp' dependency, which "
    "is not installed. Install it with:\n\n"
    '    pip install "axiom-os-lm[mcp]"\n\n'
    "(the [runtime] and [all] extras include it too). Original import error: "
    "{exc}"
)

try:
    from mcp.server import Server
    from mcp.server.lowlevel.server import InitializationOptions
    from mcp.server.stdio import stdio_server
    from mcp.types import ServerCapabilities, TextContent, Tool, ToolsCapability
except ModuleNotFoundError as exc:  # pragma: no cover - exercised via reimport test
    # Only rewrite when *mcp itself* is missing; a broken sub-import of a
    # present mcp should surface unchanged.
    if (exc.name or "").split(".")[0] != "mcp":
        raise
    raise ModuleNotFoundError(_MCP_MISSING_HINT.format(exc=exc)) from exc

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService


# ---------------------------------------------------------------------------
# Server identity + instructions for model discipline
# ---------------------------------------------------------------------------


SERVER_NAME = "axiom-memory"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """\
You have access to the user's persistent cross-tool memory ledger via this
MCP server. Use it to:

1. **At the start of a substantive task**, call `axiom_memory_recent` (or
   `axiom_memory_search`) to recall relevant prior context for the user.
   The ledger spans every tool the user has used (Claude Code, ChatGPT,
   Gemini, OpenCode, axi chat) — what was decided last week may be there.

2. **After each substantive turn**, call `axiom_memory_append` to log the
   exchange. "Substantive" means: the user shared a decision, a preference,
   a fact about themselves or their work, a project context update, or any
   exchange whose continuity will matter in a later session. Skip trivial
   greetings and routine clarifications.

3. **Do not** include PII or secrets in the `summary` field beyond what the
   user explicitly shared in conversation; the ledger is local-first but
   may flow to declared peers via federation.

4. **Provenance is mandatory.** Always pass the originating tool (e.g.
   `claude-code`, `chatgpt`, `gemini`, `axi-chat`) and the model id when
   known so cross-tool queries can scope by origin.

The ledger is the user's, not yours — write what helps them recover state
across sessions, not what helps you.
"""


# ---------------------------------------------------------------------------
# CompositionService construction — mirror of cli.py's _build_default_composition
# ---------------------------------------------------------------------------


def _build_default_composition() -> CompositionService:
    """Default-runtime CompositionService rooted in the user state dir.

    Same shape as ``cli.py::_build_default_composition``. Tests replace
    this via monkeypatch with a tmp-path-isolated service.
    """
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.infra.paths import get_user_state_dir
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.recall import RecallIndex
    from axiom.memory.trust import TrustGraph
    from axiom.rag.sqlite_store import SQLiteRAGStore
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
    # Index writes into the recall corpus so appended memory is retrievable —
    # the same recall.db build_default_serving_service() reads from. Without
    # this, appends land in the ledger but recall() serves nothing.
    store = SQLiteRAGStore(f"sqlite:///{base / 'recall.db'}")
    store.connect()
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
        recall_index=RecallIndex(store=store),
    )


# ---------------------------------------------------------------------------
# Pure tool functions — direct callable for tests; wrapped for MCP transport
# ---------------------------------------------------------------------------


def append(
    *,
    tool: str,
    principal_id: str | None = None,
    user_input: str = "",
    assistant_output: str = "",
    model: str | None = None,
    summary: str | None = None,
    scope: str = "user",
    extra: dict[str, Any] | None = None,
) -> dict:
    """Record a single conversation turn. Cross-tool common path.

    ``principal_id`` falls back to ``memory.default_principal`` when omitted.
    """
    from axiom.memory.session_capture import (
        record_session_turn,
        resolve_principal_id,
    )

    resolved_principal = resolve_principal_id(principal_id)
    composition = _build_default_composition()
    frag = record_session_turn(
        composition=composition,
        principal_id=resolved_principal,
        tool=tool,
        user_input=user_input,
        assistant_output=assistant_output,
        model=model,
        summary=summary,
        scope=scope,
        extra=extra,
    )
    return {
        "fragment_id": frag.id,
        "principal_id": resolved_principal,
        "tool": tool,
        "model": model or "",
        "event_time": frag.content.get("event_time", ""),
    }


def show(*, principal_id: str | None = None, limit: int = 10) -> dict:
    """List a principal's recent fragments (with composed summary).

    ``principal_id`` falls back to ``memory.default_principal`` when omitted.
    """
    from axiom.memory.session_capture import resolve_principal_id
    from axiom.memory.session_summary import (
        build_session_memory_summary,
        list_fragments_by_principal,
    )

    resolved = resolve_principal_id(principal_id)
    composition = _build_default_composition()
    fragments = list_fragments_by_principal(
        composition, resolved, limit=limit,
    )
    summary = build_session_memory_summary(
        composition, resolved, max_fragments=limit,
    )
    return {
        "principal": resolved,
        "fragment_count": len(fragments),
        "summary": summary,
        "fragments": [_fragment_to_dict(f) for f in fragments],
    }


def recent(*, principal_id: str | None = None, n: int = 5) -> dict:
    """Return the N most-recent turns for a principal.

    ``principal_id`` falls back to ``memory.default_principal`` when omitted.
    """
    from axiom.memory.session_capture import resolve_principal_id
    from axiom.memory.session_summary import list_fragments_by_principal

    resolved = resolve_principal_id(principal_id)
    composition = _build_default_composition()
    fragments = list_fragments_by_principal(
        composition, resolved, limit=n,
    )
    return {
        "principal": resolved,
        "fragments": [_fragment_to_dict(f) for f in fragments],
    }


def search(
    *,
    principal_id: str | None = None,
    tool: str | None = None,
    fact_kind: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> dict:
    """Filter a principal's fragments by tool / fact_kind / substring.

    ``principal_id`` falls back to ``memory.default_principal`` when omitted.
    """
    from axiom.memory.session_capture import resolve_principal_id
    from axiom.memory.session_summary import list_fragments_by_principal

    resolved = resolve_principal_id(principal_id)
    composition = _build_default_composition()
    fragments = list_fragments_by_principal(
        composition, resolved, limit=limit,
    )

    def _matches(f) -> bool:
        if tool and f.content.get("tool") != tool:
            return False
        if fact_kind and f.content.get("fact_kind") != fact_kind:
            return False
        if query:
            q = query.lower()
            blob = " ".join([
                str(f.content.get("user_input", "")),
                str(f.content.get("assistant_output", "")),
                str(f.content.get("summary", "")),
            ]).lower()
            if q not in blob:
                return False
        return True

    matched = [f for f in fragments if _matches(f)]
    return {
        "principal": resolved,
        "fragments": [_fragment_to_dict(f) for f in matched],
    }


def recall(
    *,
    query: str,
    principal_id: str | None = None,
    harness: str = "",
    account: str | None = None,
    deployment_tier: str = "local",
    model_endpoint: str = "",
    k: int = 5,
    _service: object | None = None,
) -> dict:
    """Semantic memory recall, gated by the serving boundary (ADR-087 D7 / F4).

    This is the MCP *retrieval* transport: recall → serving gate → JSON. The
    gate runs per request (vault-never, unlabeled/policy/tier/cross-account
    deny), so an MCP client can never pull memory that policy would refuse.
    ``_service`` is an injectable :class:`MemoryServingService` for tests; the
    default is the state-dir-backed service.
    """
    from axiom.extensions.builtins.memory.serving_endpoint import (
        build_default_serving_service,
        consumer_from_dict,
        mcp_recall_payload,
    )
    from axiom.memory.session_capture import resolve_principal_id

    resolved = resolve_principal_id(principal_id)
    service = _service if _service is not None else build_default_serving_service()
    consumer = consumer_from_dict({
        "principal": resolved,
        "harness": harness,
        "account": account if account is not None else resolved,
        "deployment_tier": deployment_tier,
        "model_endpoint": model_endpoint,
    })
    return mcp_recall_payload(service, query, consumer=consumer, k=k)


def actions_recent(*, n: int = 10, agent: str | None = None) -> dict:
    """Return the N most-recent agent action-provenance records.

    Thin call into ``axiom.policy.action_ledger`` (issue #665) — the
    guard-emitted ledger of every autonomous agent action, refusals
    included. Domain logic stays in the ledger module.
    """
    from axiom.policy.action_ledger import recent_actions

    return recent_actions(n=n, agent=agent)


def actions_search(
    *,
    agent: str | None = None,
    op_class: str | None = None,
    outcome: str | None = None,
    since: str | None = None,
    until: str | None = None,
    text: str | None = None,
    limit: int = 50,
) -> dict:
    """Search the agent action-provenance ledger with AND-composed filters.

    Thin call into ``axiom.policy.action_ledger`` (issue #665).
    ``since``/``until`` accept shorthand (``7d``, ``24h``) or ISO-8601.
    """
    from axiom.policy.action_ledger import search_actions

    return search_actions(
        agent=agent, op_class=op_class, outcome=outcome,
        since=since, until=until, text=text, limit=limit,
    )


def secrets_list() -> dict:
    """Foreign-credential names + metadata (issue #667). Read-only.

    Thin call into the secrets extension's metadata index — the value
    store (keychain) is never opened on this path, and values never
    appear in the payload.
    """
    from axiom.extensions.builtins.secrets.foreign.store import (
        ForeignCredentialStore,
    )
    from axiom.infra.paths import get_user_state_dir

    items = ForeignCredentialStore(get_user_state_dir()).list()
    return {"count": len(items), "items": items}


def vault_audit(*, within_days: int = 14) -> dict:
    """Credential-lifecycle audit (issue #667). Read-only metadata.

    Thin call into ``axiom.extensions.builtins.vault.audit`` — expiry
    findings over foreign credentials plus the capability-token section
    (honest unavailability until its DB query surface lands).
    """
    from axiom.extensions.builtins.vault.audit import audit_payload

    return audit_payload(within_days=within_days)


def secrets_rotate_trigger(
    *,
    name: str,
    provider: str | None = None,
    expires_at: str | None = None,
) -> dict:
    """Trigger KEEP's guarded rotation of a foreign credential (#667).

    The rotation itself is process-internal to the deterministic
    ``secrets.rotate`` skill: current value → issuer RotationProvider →
    store → probe-verify, all inside ``guarded_act`` with a #665 ledger
    journal. This tool returns the journaled action handle + outcome
    metadata — NEVER a secret value. Interactive (guided) providers fail
    closed here because MCP is headless; API-rotating providers (e.g.
    ``gitlab-pat``) complete end-to-end.
    """
    import logging as _logging

    from axiom.extensions.builtins.secrets import skills as secrets_skills
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext

    ctx = SkillContext(
        registry=secrets_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=_logging.getLogger("axiom.mcp.secrets"),
        user_prompt=None,  # headless: interactive providers fail closed
    )
    params: dict[str, Any] = {"ref": name, "surface": "mcp"}
    if provider:
        params["provider"] = provider
    if expires_at:
        params["expires_at"] = expires_at
    result = ctx.registry.invoke("secrets.rotate", params, ctx)
    return {
        "ok": result.ok,
        "value": result.value,
        "errors": result.errors,
        "actions_taken": result.actions_taken,
    }


def _fragment_to_dict(frag) -> dict:
    return {
        "id": frag.id,
        "cognitive_type": frag.cognitive_type.value,
        "timestamp": frag.provenance.timestamp,
        "tool": frag.content.get("tool", ""),
        "model": frag.content.get("model", ""),
        "fact_kind": frag.content.get("fact_kind", ""),
        "summary": frag.content.get("summary", ""),
        "user_input": frag.content.get("user_input", ""),
        "assistant_output": frag.content.get("assistant_output", ""),
    }


# ---------------------------------------------------------------------------
# MCP tool descriptors
# ---------------------------------------------------------------------------


_TOOLS: list[Tool] = [
    Tool(
        name="axiom_memory_append",
        description=(
            "Record a single conversation turn into the user's cross-tool "
            "memory ledger. Call this after substantive exchanges (decisions, "
            "preferences, context, facts) so future sessions in any tool can "
            "recall what was said. Always pass the originating `tool` so "
            "downstream queries can scope by origin."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "principal_id": {
                    "type": "string",
                    "description": (
                        "The user (e.g. 'user@example.org'). Optional — "
                        "falls back to memory.default_principal setting "
                        "when omitted."
                    ),
                },
                "tool": {
                    "type": "string",
                    "description": (
                        "Originating tool — 'claude-code', 'chatgpt', "
                        "'gemini', 'opencode', 'axi-chat'."
                    ),
                },
                "user_input": {
                    "type": "string",
                    "description": "The user's prompt for this turn.",
                },
                "assistant_output": {
                    "type": "string",
                    "description": "The assistant's response text.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Model id ('opus-4-7', 'gpt-4', 'gemini-2-flash'). "
                        "Optional but recommended."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "Compact one-line summary for prompt-injection in "
                        "future sessions. Auto-generated if omitted."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Logical scope; defaults to 'user' for personal "
                        "cross-tool memory."
                    ),
                },
                "extra": {
                    "type": "object",
                    "description": (
                        "Free-form metadata (session_id, host, cwd, etc.)."
                    ),
                },
            },
            "required": ["tool"],
        },
    ),
    Tool(
        name="axiom_memory_show",
        description=(
            "List a principal's recent memory fragments along with the "
            "composed session-memory summary that would inject into the "
            "next turn's prompt context. Read-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "principal_id": {
                    "type": "string",
                    "description": "The user to look up.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum fragments to return (default 10).",
                },
            },
        },
    ),
    Tool(
        name="axiom_memory_recent",
        description=(
            "Return the N most-recent conversation turns for a principal. "
            "Use this at session start to recall relevant prior context."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "principal_id": {"type": "string"},
                "n": {
                    "type": "integer",
                    "description": "Number of recent fragments (default 5).",
                },
            },
        },
    ),
    Tool(
        name="axiom_memory_search",
        description=(
            "Filter a principal's memory by originating tool, fact_kind, "
            "or substring match against turn text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "principal_id": {"type": "string"},
                "tool": {
                    "type": "string",
                    "description": "Filter to fragments from this tool.",
                },
                "fact_kind": {
                    "type": "string",
                    "description": "Filter by fact_kind (e.g. 'chat_turn').",
                },
                "query": {
                    "type": "string",
                    "description": "Substring match against user_input + "
                                   "assistant_output + summary.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Initial scan limit (default 50).",
                },
            },
        },
    ),
    Tool(
        name="axiom_memory_recall",
        description=(
            "Semantic recall over the user's own memory, served through the "
            "fail-closed serving gate (ADR-087 D7). Hybrid dense+sparse "
            "retrieval; every hit is policy-checked before it is returned "
            "(vault never serves; unlabeled / cross-account / wrong-deployment-"
            "tier content is denied). Pass `deployment_tier` ('local' for a "
            "self-hosted model, 'remote' for a third-party API) so tier-"
            "restricted memory never rides a prompt to a remote endpoint."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to recall (natural language).",
                },
                "principal_id": {"type": "string"},
                "harness": {
                    "type": "string",
                    "description": "Originating harness (e.g. 'claude-code').",
                },
                "account": {
                    "type": "string",
                    "description": "Consumer account; defaults to the principal.",
                },
                "deployment_tier": {
                    "type": "string",
                    "description": "'local' (self-hosted) or 'remote' (3rd-party API).",
                },
                "k": {
                    "type": "integer",
                    "description": "Max fragments to return (default 5).",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="axiom_actions_recent",
        description=(
            "Return the N most-recent records from the agent action-"
            "provenance ledger — every autonomous agent action that went "
            "through the platform action guard (branch prunes, artifact "
            "cleanups, auto-closes, ...), refusals included. Use this to "
            "answer 'what did the agents just do?'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Number of recent records (default 10).",
                },
                "agent": {
                    "type": "string",
                    "description": (
                        "Scope to one agent (e.g. 'tidy', 'rivet', "
                        "'plinth'). Optional."
                    ),
                },
            },
        },
    ),
    Tool(
        name="axiom_actions_search",
        description=(
            "Search the agent action-provenance ledger. Filters AND-"
            "compose: agent, op_class (e.g. 'git.branch.delete'), outcome "
            "('proceeded' / 'refused' / 'failed'), a since/until time "
            "window, and substring text match. Refused actions carry the "
            "refusing rule; completed ones may carry an undo handle. Use "
            "this to answer 'what did TIDY do yesterday?' or 'why was "
            "that action refused?'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Filter to one agent (e.g. 'tidy').",
                },
                "op_class": {
                    "type": "string",
                    "description": (
                        "Filter by operation class (e.g. "
                        "'git.branch.delete', 'github.issue.close')."
                    ),
                },
                "outcome": {
                    "type": "string",
                    "description": (
                        "'proceeded', 'refused', or 'failed'."
                    ),
                },
                "since": {
                    "type": "string",
                    "description": (
                        "Window floor: shorthand (Nm/Nh/Nd/Nw, e.g. '24h') "
                        "or ISO-8601."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": (
                        "Window ceiling: shorthand or ISO-8601."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": (
                        "Substring match against candidate, action name, "
                        "refusing rule, and undo handle."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum records to return (default 50).",
                },
            },
        },
    ),
    Tool(
        name="axiom_secrets_list",
        description=(
            "List the user's foreign credentials (GitLab PATs, webhook "
            "URLs, HMAC keys) — names and metadata (provider kind, issuer, "
            "expiry, git wiring) ONLY. Secret values never transit MCP; "
            "the keychain is not even opened for this call."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="axiom_vault_audit",
        description=(
            "Credential-lifecycle audit across KEEP's two planes: expiry "
            "findings for foreign credentials (expired / expiring / "
            "no_expiry) and the Axiom-minted capability section. Read-only "
            "metadata — use it to answer 'which credentials need rotation "
            "soon?'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "within_days": {
                    "type": "integer",
                    "description": (
                        "Horizon for 'expiring' findings (default 14)."
                    ),
                },
            },
        },
    ),
    Tool(
        name="axiom_secrets_rotate_trigger",
        description=(
            "Trigger KEEP's guarded rotation of a named foreign "
            "credential. The exchange (read current → issuer rotation API "
            "→ store new → probe-verify) is process-internal to the "
            "deterministic skill and journals to the action ledger; this "
            "tool returns the journaled action handle, outcome metadata, "
            "and scrub candidates — NEVER a secret value. Providers "
            "without API rotation (guided/interactive) fail closed over "
            "MCP: run `axi secrets rotate <name>` in a terminal instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Foreign credential name (see axiom_secrets_list).",
                },
                "provider": {
                    "type": "string",
                    "description": (
                        "RotationProvider kind override (e.g. 'gitlab-pat'); "
                        "defaults to the credential's stored provider."
                    ),
                },
                "expires_at": {
                    "type": "string",
                    "description": (
                        "Expiry for the replacement (YYYY-MM-DD; issuer "
                        "default is +1 week)."
                    ),
                },
            },
            "required": ["name"],
        },
    ),
]


_HANDLERS: dict = {
    "axiom_memory_append": lambda args: append(**args),
    "axiom_memory_show": lambda args: show(**args),
    "axiom_memory_recent": lambda args: recent(**args),
    "axiom_memory_search": lambda args: search(**args),
    "axiom_memory_recall": lambda args: recall(**args),
    "axiom_actions_recent": lambda args: actions_recent(**args),
    "axiom_actions_search": lambda args: actions_search(**args),
    "axiom_secrets_list": lambda args: secrets_list(**args),
    "axiom_vault_audit": lambda args: vault_audit(**args),
    "axiom_secrets_rotate_trigger": lambda args: secrets_rotate_trigger(**args),
}


# ---------------------------------------------------------------------------
# MCP server wiring
# ---------------------------------------------------------------------------


def build_server() -> Server:
    """Construct the axiom-memory MCP Server with tools + handlers wired."""
    from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult

    async def _list_tools(_ctx: Any, _params: Any = None) -> ListToolsResult:
        return ListToolsResult(tools=_TOOLS)

    async def _call_tool(_ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        handler = _HANDLERS.get(params.name)
        if handler is None:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps({
                "error": f"unknown tool: {params.name}",
            }))])
        try:
            result = handler(params.arguments or {})
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, indent=2))]
        )

    server: Server = Server(
        SERVER_NAME,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )

    return server


def _initialization_options(server: Server) -> InitializationOptions:
    """Build initialization options carrying our `instructions` field."""
    return InitializationOptions(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        capabilities=ServerCapabilities(tools=ToolsCapability()),
        instructions=INSTRUCTIONS,
    )


async def run() -> None:
    """Serve MCP over stdio. Entry point for `python -m <this module>`."""
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, _initialization_options(server),
        )


if __name__ == "__main__":
    asyncio.run(run())
