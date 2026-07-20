# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi memory`` — inspect, record, and migrate the per-principal memory ledger.

Reads, writes, and migrations all flow through CompositionService.
PostgreSQL (or SQLite for smaller nodes) remains the source of truth.

Subcommands:

- ``axi memory show <principal>`` — list the N most-recent episodic
  fragments for a principal, with the composed session-memory
  summary that would flow into a new chat turn.
- ``axi memory record`` — record a single conversation turn into the
  ledger. Same write path the axiom-memory MCP server's append tool
  uses. Accepts flags or JSON-on-stdin (``--json-stdin``); shell
  automation, hooks, and ingest backstops all share this entry point.
- ``axi memory migrate --backfill-accountable-human <scope>`` — walk
  pre-bump (v1) fragments in the scope and assign accountable humans
  where the chain can be unambiguously inferred per ADR-035 §D7.
  Tombstone the original; write a v2 fragment that supersedes it.
- ``axi memory forget`` — redact fragments from recall (soft-delete /
  tombstone), gated on Right.CONTROL, with an audit trail. Select by
  explicit fragment id(s) or by ``--principal`` narrowed with ``--match``
  (a full-history purge requires ``--all``); ``--dry-run`` previews.
- ``axi memory absorb`` — ingest a harness's native memory (ADR-087
  D8): read-only adapter scan → origin-stamped fragments via the D2
  import primitive. Re-absorb is a no-op.
- ``axi memory conflicts list`` — the kept-both dedup conflict queue,
  read-only in P2 (no adjudication verbs yet).
- ``axi memory dedup recluster`` — invocable corpus-health
  entity-resolution pass (no scheduler wiring in P2).

Summary regeneration and hard (crypto-shred) erasure remain follow-ons.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from axiom.setup.renderer import _c, _Colors, _use_color


def _h1(text: str) -> str:
    """Top-level header — bold accent-blue when color, raw otherwise."""
    return _c(_Colors.BOLD + _Colors.ACCENT_BLUE, text) if _use_color() else f"# {text}"


def _h2(text: str) -> str:
    """Subheader — bold when color, raw otherwise."""
    return _c(_Colors.BOLD, text) if _use_color() else f"## {text}"


def _bold(text: str) -> str:
    return _c(_Colors.BOLD, text) if _use_color() else f"**{text}**"


def _code_inline(text: str) -> str:
    return _c(_Colors.CYAN, text) if _use_color() else f"`{text}`"


def _dim_italic(text: str) -> str:
    """Italic-via-dim. Pure terminals lack italic; dim is the closest semantic."""
    return _c(_Colors.DIM, text) if _use_color() else f"_{text}_"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi memory",
        description="Inspect, record, and migrate a principal's memory ledger.",
    )
    sub = parser.add_subparsers(dest="action")

    p_show = sub.add_parser(
        "show",
        help="Show a principal's prior-session fragments.",
    )
    p_show.add_argument(
        "principal",
        nargs="?",
        default=None,
        help=(
            "Principal id (e.g. @alice:demo, user@example.org). "
            "Falls back to memory.default_principal setting when omitted."
        ),
    )
    p_show.add_argument(
        "--limit", type=int, default=10,
        help="Maximum fragments to display (default: 10).",
    )
    p_show.add_argument(
        "--classroom-id", default="", dest="classroom_id",
        help=(
            "Optional classroom id to scope the read to a classroom's "
            "composition service. Defaults to the default runtime "
            "composition service."
        ),
    )
    p_show.add_argument("--json", action="store_true", default=False)

    # ----- record -----------------------------------------------------------
    p_record = sub.add_parser(
        "record",
        help="Record a conversation turn into the ledger.",
        description=(
            "Append a single conversation turn through the same common "
            "path the MCP write tool and axi chat use. Provide values "
            "via flags, or pipe a JSON event on stdin via --json-stdin."
        ),
    )
    p_record.add_argument(
        "--principal", dest="principal_id", default=None,
        help="Principal id (e.g. user@example.org). Required unless --json-stdin.",
    )
    p_record.add_argument(
        "--tool", default=None,
        help=(
            "Originating tool — claude-code, chatgpt, gemini, opencode, "
            "axi-chat. Required unless --json-stdin."
        ),
    )
    p_record.add_argument(
        "--model", default=None,
        help="Model id (opus-4-7, gpt-4, gemini-2-flash). Optional.",
    )
    p_record.add_argument(
        "--user-input", dest="user_input", default="",
        help="The user's prompt for this turn.",
    )
    p_record.add_argument(
        "--assistant-output", dest="assistant_output", default="",
        help="The assistant's response text.",
    )
    p_record.add_argument(
        "--summary", default=None,
        help="Optional one-line summary; auto-generated if omitted.",
    )
    p_record.add_argument(
        "--scope", default="user",
        help="Logical scope (default: 'user' for cross-tool memory).",
    )
    p_record.add_argument(
        "--json-stdin", dest="json_stdin", action="store_true", default=False,
        help="Read a JSON event from stdin instead of using flags.",
    )
    p_record.add_argument(
        "--json", action="store_true", default=False,
        help="Emit the resulting fragment summary as JSON.",
    )

    # ----- forget -----------------------------------------------------------
    p_forget = sub.add_parser(
        "forget",
        help="Redact fragments from recall (soft-delete / tombstone).",
        description=(
            "Remove fragments from a principal's recall. Redaction, not "
            "erasure: the row + reason are kept for audit; reads and recall "
            "exclude it. Requires Right.CONTROL over each fragment (the master "
            "always has it). Select by explicit fragment id(s), or by "
            "--principal narrowed with --match; forgetting a principal's "
            "entire history requires --all."
        ),
    )
    p_forget.add_argument(
        "fragment_ids", nargs="*",
        help="Explicit fragment id(s) to forget (optional).",
    )
    p_forget.add_argument(
        "--principal", default=None,
        help=(
            "Principal whose memory you're managing — the caller-asserted "
            "requester for the CONTROL check (as with show/record)."
        ),
    )
    p_forget.add_argument(
        "--match", default=None,
        help="Only forget fragments whose content contains this substring.",
    )
    p_forget.add_argument(
        "--all", action="store_true", default=False, dest="forget_all",
        help="Confirm a full purge of the principal's fragments (no --match).",
    )
    p_forget.add_argument(
        "--reason", default="forget",
        help="Deletion reason recorded on the tombstone + audit entry.",
    )
    p_forget.add_argument(
        "--dry-run", action="store_true", default=False, dest="dry_run",
        help="Report what would be forgotten without changing anything.",
    )
    p_forget.add_argument(
        "--classroom-id", default="", dest="classroom_id",
        help="Scope to a classroom's composition service (default: runtime).",
    )
    p_forget.add_argument("--json", action="store_true", default=False)

    # ----- export -----------------------------------------------------------
    p_export = sub.add_parser(
        "export",
        help="Export a principal's memory as a signed portable bundle.",
        description=(
            "Write a signed tar.gz bundle (fragments + session checkpoints "
            "+ audit slice + manifest) for moving this principal's memory "
            "to another account or node (ADR-087 D9). Vault content is "
            "never exported in plaintext."
        ),
    )
    p_export.add_argument(
        "--principal", default=None,
        help=(
            "Principal whose memory to export. Falls back to the "
            "memory.default_principal setting when omitted."
        ),
    )
    p_export.add_argument(
        "--out", required=True,
        help="Path for the bundle (e.g. ~/alice-memory.tar.gz).",
    )
    p_export.add_argument(
        "--include-vault", action="store_true", default=False,
        dest="include_vault",
        help=(
            "Refused: opt-in re-encrypted vault export is not yet "
            "supported; vault fragments are always excluded."
        ),
    )
    p_export.add_argument(
        "--sessions-dir", default=None, dest="sessions_dir",
        help="Override the session-checkpoint directory (default: state dir).",
    )
    p_export.add_argument("--json", action="store_true", default=False)

    # ----- import -----------------------------------------------------------
    p_import = sub.add_parser(
        "import",
        help="Import a signed bundle, re-homing it to a new principal.",
        description=(
            "Verify a portable bundle (member hashes + manifest signature; "
            "fail-closed) and re-home its fragments to the assumed "
            "principal via the ADR-026 dual-signature ceremony. Re-import "
            "of the same bundle is a no-op."
        ),
    )
    p_import.add_argument(
        "bundle", help="Path to a bundle produced by `axi memory export`.",
    )
    p_import.add_argument(
        "--assume-principal", required=True, dest="assume_principal",
        help=(
            "Destination identity that becomes the new master of every "
            "imported fragment (e.g. your work-account principal)."
        ),
    )
    p_import.add_argument(
        "--dry-run", action="store_true", default=False, dest="dry_run",
        help="Verify + report what would be imported without writing.",
    )
    p_import.add_argument(
        "--sessions-dir", default=None, dest="sessions_dir",
        help=(
            "Override where session checkpoints are restored "
            "(default: state dir)."
        ),
    )
    p_import.add_argument("--json", action="store_true", default=False)

    # ----- absorb -----------------------------------------------------------
    p_absorb = sub.add_parser(
        "absorb",
        help="Absorb a harness's native memory into the ledger (read-only scan).",
        description=(
            "Scan one harness-native memory store with its read-only "
            "adapter and land the memories through the D2 import "
            "primitive, stamped with their SourceOrigin coordinate. "
            "Re-absorb is a no-op; content drift under a stable source "
            "ref is kept-both and queued as a conflict; the source store "
            "is never written."
        ),
    )
    p_absorb.add_argument(
        "--harness", required=True,
        help=(
            "Source memory model: agents-md | claude-code | codex | "
            "gemini-cli | goose | hermes | letta."
        ),
    )
    p_absorb.add_argument(
        "--account", default="local",
        help="Opaque provider-scoped account label (default: local).",
    )
    p_absorb.add_argument(
        "--principal", default=None,
        help=(
            "Owner of the absorbed memories. Falls back to the "
            "memory.default_principal setting when omitted."
        ),
    )
    p_absorb.add_argument(
        "--home", default=None,
        help="Override the harness home directory (claude-code, gemini-cli, codex, hermes).",
    )
    p_absorb.add_argument(
        "--root", action="append", dest="roots", default=None,
        help="Project root for hierarchy walks (repeatable; required for agents-md).",
    )
    p_absorb.add_argument(
        "--path", default=None,
        help="Store path: goose memory dir / letta sqlite db.",
    )
    p_absorb.add_argument(
        "--no-dedup", action="store_true", dest="no_dedup", default=False,
        help="Skip the write-time near-neighbor check; land candidates verbatim.",
    )
    p_absorb.add_argument(
        "--dry-run", action="store_true", dest="dry_run", default=False,
        help="Scan + classify; write nothing.",
    )
    p_absorb.add_argument("--json", action="store_true", default=False)

    # ----- conflicts --------------------------------------------------------
    p_conflicts = sub.add_parser(
        "conflicts",
        help="Inspect the kept-both dedup conflict queue (read-only).",
        description=(
            "Conflicting/ambiguous memories are kept both and queued, "
            "never auto-merged (ADR-087 D3). P2 exposes the queue "
            "read-only; adjudication verbs are a later knob decision."
        ),
    )
    p_conflicts.add_argument(
        "subaction", choices=["list"],
        help="Only 'list' exists in P2 — the queue is read-only.",
    )
    p_conflicts.add_argument(
        "--principal", default=None,
        help="Filter to one principal's conflicts (default: all).",
    )
    p_conflicts.add_argument("--json", action="store_true", default=False)

    # ----- dedup ------------------------------------------------------------
    p_dedup = sub.add_parser(
        "dedup",
        help="Entity-resolution passes over a principal's memory.",
        description=(
            "`recluster` runs the invocable corpus-health pass: exact/"
            "near-duplicate clusters fold reversibly into their earliest "
            "fragment, ambiguous pairs queue as conflicts, vault and "
            "open conflicts are never touched. No scheduler wiring in P2 "
            "— invoke it explicitly."
        ),
    )
    p_dedup.add_argument(
        "subaction", choices=["recluster"],
        help="Only 'recluster' exists in P2.",
    )
    p_dedup.add_argument(
        "--principal", default=None,
        help=(
            "Principal whose memory to recluster. Falls back to the "
            "memory.default_principal setting when omitted."
        ),
    )
    p_dedup.add_argument(
        "--dry-run", action="store_true", dest="dry_run", default=False,
        help="Report what would merge/queue without changing anything.",
    )
    p_dedup.add_argument("--json", action="store_true", default=False)

    # ----- ingest -----------------------------------------------------------
    p_ingest = sub.add_parser(
        "ingest",
        help="Ingest a Claude Code session transcript into the ledger.",
        description=(
            "Walk a Claude Code session JSONL transcript and fold each "
            "user/assistant turn pair into the principal's memory ledger "
            "via the same common path `axi memory record` and the MCP "
            "write tool use. Lossless backstop for tools whose session "
            "logs are accessible on disk; complements model-driven MCP "
            "writes by capturing turns the model didn't think to log."
        ),
    )
    p_ingest.add_argument(
        "path", help="Path to a Claude Code session .jsonl file.",
    )
    p_ingest.add_argument(
        "--principal", dest="principal_id", default=None,
        help=(
            "Principal id to attribute the fragments to. Falls back to "
            "memory.default_principal setting when omitted."
        ),
    )
    p_ingest.add_argument(
        "--tool", default="claude-code",
        help=(
            "Originating tool whose transcript format to parse. "
            "Supported: claude-code (canonical). Stubs that raise with "
            "a contributor pointer: opencode, gemini, chatgpt-desktop. "
            "Default: claude-code."
        ),
    )
    p_ingest.add_argument(
        "--limit", type=int, default=None,
        help="Cap on turn pairs ingested (default: no cap).",
    )
    p_ingest.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Report counts only; don't write fragments.",
    )
    p_ingest.add_argument(
        "--watch", action="store_true", default=False,
        help=(
            "Poll the transcript on a fixed interval and ingest new "
            "turn pairs as they appear. Idempotent — re-running across "
            "iterations is safe. Ctrl+C to stop."
        ),
    )
    p_ingest.add_argument(
        "--interval", type=float, default=5.0,
        help="Polling interval in seconds for --watch (default 5).",
    )
    p_ingest.add_argument(
        "--max-iterations", dest="max_iterations", type=int, default=None,
        help=(
            "Cap the number of polling cycles in --watch mode "
            "(default: unbounded). Mostly a testing affordance."
        ),
    )
    p_ingest.add_argument(
        "--json", action="store_true", default=False,
        help="Emit the count report as JSON.",
    )

    # ----- register-mcp -----------------------------------------------------
    p_register = sub.add_parser(
        "register-mcp",
        help="Register the axiom-memory MCP server in user-scope Claude config.",
        description=(
            "Add (or update) the axiom-memory entry in ~/.claude.json so "
            "every Claude Code session on this machine reaches the MCP. "
            "Idempotent — safe to run repeatedly. Uses sys.executable so "
            "the entry follows the venv that ran the command."
        ),
    )
    p_register.add_argument(
        "--check", action="store_true", default=False,
        help=(
            "Detection-only: exit 0 if registered, non-zero if missing. "
            "Used by axi dr."
        ),
    )
    p_register.add_argument(
        "--all", action="store_true", dest="all_tools", default=False,
        help=(
            "Register the axiom-memory MCP in every detected LLM tool's "
            "user-scope config (Claude Code, Codex, Gemini, OpenCode). "
            "Tools that aren't detected are skipped; not-yet-implemented "
            "tools surface a contributor pointer."
        ),
    )
    p_register.add_argument(
        "--tool", default=None,
        help=(
            "Limit the operation to a single tool by name. Default: "
            "claude-code (back-compat for the existing one-tool flow)."
        ),
    )
    p_register.add_argument(
        "--json", action="store_true", default=False,
        help="Emit the result as JSON.",
    )

    # ----- heartbeat --------------------------------------------------------
    p_heartbeat = sub.add_parser(
        "heartbeat",
        help="Write a single heartbeat fragment (used by cron / launchd).",
        description=(
            "Write one heartbeat fragment to the ledger. Designed to be "
            "invoked on a fixed cadence (default: hourly via launchd). "
            "axi dr's heartbeat-freshness check uses the most-recent "
            "heartbeat to detect a broken write path."
        ),
    )
    p_heartbeat.add_argument(
        "--principal", dest="principal_id", default=None,
        help="Principal id; falls back to memory.default_principal pin.",
    )
    p_heartbeat.add_argument(
        "--source", default="axi-monitor",
        help="Provenance source label; default 'axi-monitor'.",
    )
    p_heartbeat.add_argument(
        "--json", action="store_true", default=False,
        help="Emit the resulting fragment id as JSON.",
    )

    # ----- heartbeat-install / heartbeat-uninstall (macOS launchd) ----------
    p_hb_install = sub.add_parser(
        "heartbeat-install",
        help="Install the macOS launchd plist that runs `axi memory heartbeat` hourly.",
        description=(
            "Write ~/Library/LaunchAgents/com.axiom.memory.heartbeat.plist "
            "and load it via launchctl so the heartbeat fires on a fixed "
            "cadence (default: hourly). Idempotent — re-running rewrites "
            "the plist with current values."
        ),
    )
    p_hb_install.add_argument(
        "--interval", type=int, default=3600,
        help="Polling interval in seconds (default 3600 = hourly).",
    )
    p_hb_install.add_argument(
        "--no-load", dest="no_load", action="store_true", default=False,
        help="Write the plist but skip `launchctl load` (debugging).",
    )
    p_hb_install.add_argument(
        "--json", action="store_true", default=False,
        help="Emit the install report as JSON.",
    )

    p_hb_uninstall = sub.add_parser(
        "heartbeat-uninstall",
        help="Unload + remove the launchd plist installed by heartbeat-install.",
    )
    p_hb_uninstall.add_argument(
        "--no-unload", dest="no_unload", action="store_true", default=False,
        help="Remove the plist file but skip `launchctl unload` (debugging).",
    )
    p_hb_uninstall.add_argument(
        "--json", action="store_true", default=False,
        help="Emit the uninstall report as JSON.",
    )

    # ----- migrate ----------------------------------------------------------
    p_migrate = sub.add_parser(
        "migrate",
        help="Migrate fragments to a newer schema version (ADR-035).",
    )
    p_migrate.add_argument(
        "--backfill-accountable-human",
        dest="backfill_scope",
        metavar="SCOPE_ID",
        help=(
            "Walk v1 fragments in SCOPE_ID and assign accountable humans. "
            "Per ADR-035 §D7 + memory-persistence-plan §5: original is "
            "tombstoned (reason=migrated_to_v2); a new v2 fragment "
            "supersedes."
        ),
    )
    p_migrate.add_argument(
        "--default-human",
        dest="default_human",
        default=None,
        help=(
            "Fallback human principal for fragments whose actor is an "
            "agent and cannot be unambiguously linked back to a human. "
            "When omitted, ambiguous fragments are flagged in the audit "
            "projection and skipped."
        ),
    )
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report counts only; don't write or tombstone.",
    )
    p_migrate.add_argument(
        "--classroom-id", default="", dest="classroom_id",
        help=(
            "Optional classroom id to scope the migration to a classroom's "
            "composition service (default: runtime composition service)."
        ),
    )
    p_migrate.add_argument("--json", action="store_true", default=False)

    # ----- reindex ----------------------------------------------------------
    p_reindex = sub.add_parser(
        "reindex",
        help="Rebuild the semantic recall corpus from the ledger (backfill).",
        description=(
            "Re-project the ledger into the recall corpus (recall.db). The "
            "corpus is a disposable read-side projection (ADR-088 §5); "
            "fragments recorded before the append→recall index was wired sit "
            "in the ledger but were never projected, so `axi memory recall` / "
            "the MCP recall tool serve nothing for them. This backfills them. "
            "Idempotent — safe to re-run. Vault is never projected."
        ),
    )
    p_reindex.add_argument(
        "--principal", default=None,
        help=(
            "Principal whose corpus to rebuild. Falls back to "
            "memory.default_principal when omitted. Ignored with --all."
        ),
    )
    p_reindex.add_argument(
        "--all", action="store_true", dest="all_principals", default=False,
        help="Reindex every principal found in the ledger.",
    )
    p_reindex.add_argument("--json", action="store_true", default=False)

    parser.add_argument("--json", action="store_true", help="Output as JSON")
    return parser


def _build_composition_for_classroom(classroom_id: str):
    from axiom.extensions.builtins.classroom.composition_boot import (
        build_classroom_composition,
    )

    return build_classroom_composition(classroom_id)


def _build_default_composition():
    """Default runtime composition service (fallback when no classroom).

    Uses the user state dir so instructor / non-classroom principals can
    inspect their own memory without a classroom context.
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
    # Index writes into the recall corpus so recorded memory is retrievable —
    # the same recall.db build_default_serving_service() reads from. Without
    # this, records land in the ledger but recall() serves nothing.
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


def _cmd_show(args: argparse.Namespace) -> int:
    from axiom.memory.session_capture import resolve_principal_id
    from axiom.memory.session_summary import (
        build_session_memory_summary,
        list_fragments_by_principal,
    )

    try:
        principal = resolve_principal_id(args.principal)
    except ValueError as exc:
        print(f"axi memory show: {exc}", file=sys.stderr)
        return 1

    if args.classroom_id:
        composition = _build_composition_for_classroom(args.classroom_id)
    else:
        composition = _build_default_composition()

    fragments = list_fragments_by_principal(
        composition, principal, limit=args.limit,
    )
    summary = build_session_memory_summary(
        composition, principal, max_fragments=args.limit,
    )

    payload = {
        "principal": principal,
        "fragment_count": len(fragments),
        "summary": summary,
        "fragments": [
            {
                "id": f.id,
                "cognitive_type": f.cognitive_type.value,
                "timestamp": f.provenance.timestamp,
                "fact_kind": f.content.get("fact_kind", ""),
                "summary": f.content.get("summary", ""),
            }
            for f in fragments
        ],
    }

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(_h1(f"Memory for {principal}"))
    print()
    if not fragments:
        print(
            _dim_italic(
                "No prior fragments. Once this principal starts chatting, "
                "session events will accumulate here."
            )
        )
        return 0

    n = len(fragments)
    plural = "fragment" if n == 1 else "fragments"
    print(f"{_bold(f'{n} {plural}')} — most recent first:")
    print()
    for f in fragments:
        when = f.provenance.timestamp[:19]
        kind = f.content.get("fact_kind") or "(untyped)"
        summary_line = (f.content.get("summary") or "").strip()
        bullet = _c(_Colors.CYAN, "-") if _use_color() else "-"
        if summary_line:
            print(f"  {bullet} {_code_inline(when)} {_bold(kind)} — {summary_line}")
        else:
            print(f"  {bullet} {_code_inline(when)} {_bold(kind)} — {_dim_italic('(no summary)')}")
    print()
    if summary:
        print(_h2("Composed session-memory summary"))
        print(_dim_italic("This is what would be injected into the next turn's prompt."))
        print()
        for line in summary.splitlines():
            print(f"  {_c(_Colors.DIM, line) if _use_color() else line}")
    return 0


# ---------------------------------------------------------------------------
# Forget — redaction path (thin wrapper over the memory.forget skill)
# ---------------------------------------------------------------------------


def _cmd_forget(args: argparse.Namespace) -> int:
    """Thin ADR-056 wrapper: build the composition, invoke the forget skill."""
    from .skills.forget import forget as forget_skill

    if args.classroom_id:
        composition = _build_composition_for_classroom(args.classroom_id)
    else:
        composition = _build_default_composition()

    params = {
        "composition": composition,
        "fragment_ids": args.fragment_ids,
        "principal": args.principal,
        "match": args.match,
        "all": args.forget_all,
        "reason": args.reason,
        "dry_run": args.dry_run,
        "requester": args.principal,
    }
    result = forget_skill(params, None)

    if args.json:
        payload = result.value if result.value is not None else {
            "errors": result.errors
        }
        print(json.dumps(payload, indent=2))
        return result.exit_code

    if result.value is None:
        for err in result.errors:
            print(f"axi memory forget: {err}", file=sys.stderr)
        return result.exit_code

    v = result.value
    if v.get("dry_run"):
        ids = v.get("would_forget", [])
        print(_h1(f"Would forget {len(ids)} fragment(s)"))
        for fid in ids:
            print(f"  - {_code_inline(fid)}")
        if not ids:
            print(_dim_italic("Nothing matches."))
        return 0

    if v.get("note"):
        print(_dim_italic(v["note"]))
        return result.exit_code

    n = v.get("count", 0)
    plural = "fragment" if n == 1 else "fragments"
    print(f"{_bold(f'Forgot {n} {plural}')} (reason: {v.get('reason', 'forget')}).")
    denied = v.get("denied") or []
    not_found = v.get("not_found") or []
    if denied:
        print(_dim_italic(f"  {len(denied)} denied (no CONTROL): {', '.join(denied)}"))
    if not_found:
        print(_dim_italic(f"  {len(not_found)} not found: {', '.join(not_found)}"))
    return result.exit_code


# ---------------------------------------------------------------------------
# Export / import — portable bundles (thin wrappers over the skills)
# ---------------------------------------------------------------------------


def _cmd_export(args: argparse.Namespace) -> int:
    """Thin ADR-056 wrapper: build the composition, invoke memory.export."""
    from axiom.memory.session_capture import resolve_principal_id

    from .skills.export_bundle import export_bundle

    try:
        principal = resolve_principal_id(args.principal)
    except ValueError as exc:
        print(f"axi memory export: {exc}", file=sys.stderr)
        return 1

    result = export_bundle({
        "composition": _build_default_composition(),
        "principal": principal,
        "out": args.out,
        "include_vault": args.include_vault,
        "sessions_dir": args.sessions_dir,
    }, None)

    if args.json and result.value is not None:
        print(json.dumps(result.value, indent=2))
        return result.exit_code
    if not result.ok:
        for err in result.errors:
            print(f"axi memory export: {err}", file=sys.stderr)
        return result.exit_code

    v = result.value
    counts = v["counts"]
    print(_h1(f"Exported memory for {v['principal']}"))
    print()
    print(f"- Bundle: {_code_inline(v['bundle'])}")
    print(f"- Fragments: {counts['fragments']}")
    print(f"- Session checkpoints: {counts['sessions']}")
    print(f"- Audit entries: {counts['audit_entries']}")
    if counts["vault_excluded"]:
        print(_dim_italic(
            f"  {counts['vault_excluded']} vault fragment(s) excluded — "
            "vault never rides a bundle."
        ))
    return result.exit_code


def _cmd_import(args: argparse.Namespace) -> int:
    """Thin ADR-056 wrapper: build the composition, invoke memory.import."""
    from .skills.import_bundle import import_bundle

    result = import_bundle({
        "composition": _build_default_composition(),
        "bundle": args.bundle,
        "assume_principal": args.assume_principal,
        "dry_run": args.dry_run,
        "sessions_dir": args.sessions_dir,
    }, None)

    if args.json and result.value is not None:
        print(json.dumps(result.value, indent=2))
        return result.exit_code
    if not result.ok:
        for err in result.errors:
            print(f"axi memory import: {err}", file=sys.stderr)
        return result.exit_code

    v = result.value
    if v.get("dry_run"):
        print(_h1("Import dry run"))
        print()
        print(f"- Would import: {v['would_import']}")
        print(f"- Already present (skipped): {v['skipped_duplicate']}")
        print(f"- Conflicts: {len(v['conflicts'])}")
        print(f"- Session checkpoints: {v['sessions']}")
        return 0

    print(_h1(f"Imported memory as {v['assume_principal']}"))
    print()
    print(f"- From: {v['from_principal']}")
    print(f"- Imported: {v['imported']}")
    print(f"- Already present (skipped): {v['skipped_duplicate']}")
    print(f"- Session checkpoints restored: {v['sessions_imported']}")
    if v["conflicts"]:
        print(_dim_italic(
            f"  {len(v['conflicts'])} conflict(s) kept-existing: "
            f"{', '.join(v['conflicts'])}"
        ))
    return result.exit_code


# ---------------------------------------------------------------------------
# Absorb / conflicts / dedup — thin ADR-056 wrappers over the P2 skills
# ---------------------------------------------------------------------------


def _cmd_absorb(args: argparse.Namespace) -> int:
    """Thin ADR-056 wrapper: build the composition, invoke memory.absorb."""
    from axiom.memory.session_capture import resolve_principal_id

    from .skills.absorb import absorb

    try:
        principal = resolve_principal_id(args.principal)
    except ValueError as exc:
        print(f"axi memory absorb: {exc}", file=sys.stderr)
        return 1

    result = absorb({
        "composition": _build_default_composition(),
        "harness": args.harness,
        "account": args.account,
        "principal": principal,
        "home": args.home,
        "roots": args.roots,
        "path": args.path,
        "no_dedup": args.no_dedup,
        "dry_run": args.dry_run,
    }, None)

    if args.json and result.value is not None:
        print(json.dumps(result.value, indent=2))
        return result.exit_code
    if not result.ok:
        for err in result.errors:
            print(f"axi memory absorb: {err}", file=sys.stderr)
        return result.exit_code

    v = result.value
    title = "Absorb dry run" if v["dry_run"] else "Absorbed"
    print(_h1(f"{title} — {v['harness']} → {v['principal']}"))
    print()
    print(f"- Candidates scanned: {v['candidates']}")
    print(f"- Imported: {v['imported']}")
    print(f"- Already present (echo-suppressed): {v['skipped_echo']}")
    if v["collapsed_exact"]:
        print(f"- Collapsed exact duplicates: {v['collapsed_exact']}")
    if v["merged_near_dup"]:
        print(f"- Near-duplicates merged (reversible): {v['merged_near_dup']}")
    if v["conflicts_queued"]:
        print(f"- Conflicts queued (kept both): {v['conflicts_queued']}")
        print(_dim_italic("  review with: axi memory conflicts list"))
    if v["skipped"]:
        print(_dim_italic(f"  {len(v['skipped'])} source(s) skipped:"))
        for s in v["skipped"]:
            print(_dim_italic(f"    - {s['source']}: {s['reason']}"))
    return result.exit_code


def _cmd_conflicts(args: argparse.Namespace) -> int:
    """Thin ADR-056 wrapper: invoke memory.conflicts_list (read-only)."""
    from .skills.conflicts_list import conflicts_list

    result = conflicts_list({
        "composition": _build_default_composition(),
        "principal": args.principal,
    }, None)

    if args.json and result.value is not None:
        print(json.dumps(result.value, indent=2))
        return result.exit_code
    if not result.ok:
        for err in result.errors:
            print(f"axi memory conflicts: {err}", file=sys.stderr)
        return result.exit_code

    v = result.value
    n = v["count"]
    scope = f" for {v['principal']}" if v["principal"] else ""
    print(_h1(f"Memory conflicts{scope}: {n} open"))
    print()
    if not n:
        print(_dim_italic("Queue is empty — nothing awaiting review."))
        return 0
    for entry in v["conflicts"]:
        ids = ", ".join(entry.get("fragment_ids", []))
        when = (entry.get("detected_at") or "")[:19]
        print(f"  - {_code_inline(when)} {entry.get('reason', '')}: {ids}")
    print()
    print(_dim_italic(
        "Kept-both, never auto-merged. Adjudication verbs land in a "
        "later phase; the queue is read-only for now."
    ))
    return 0


def _cmd_dedup(args: argparse.Namespace) -> int:
    """Thin ADR-056 wrapper: invoke memory.dedup_recluster."""
    from axiom.memory.session_capture import resolve_principal_id

    from .skills.dedup_recluster import dedup_recluster

    try:
        principal = resolve_principal_id(args.principal)
    except ValueError as exc:
        print(f"axi memory dedup: {exc}", file=sys.stderr)
        return 1

    result = dedup_recluster({
        "composition": _build_default_composition(),
        "principal": principal,
        "dry_run": args.dry_run,
    }, None)

    if args.json and result.value is not None:
        print(json.dumps(result.value, indent=2))
        return result.exit_code
    if not result.ok:
        for err in result.errors:
            print(f"axi memory dedup: {err}", file=sys.stderr)
        return result.exit_code

    v = result.value
    title = "Recluster dry run" if v["dry_run"] else "Recluster"
    print(_h1(f"{title} — {v['principal']}"))
    print()
    print(f"- Fragments examined: {v['fragments']}")
    print(f"- Pairs matched: {v['examined_pairs']}")
    print(f"- Clusters folded: {v['clusters']}")
    print(f"- Merged (reversible): {v['merged']}")
    print(f"- Conflicts queued (kept both): {v['conflicts_queued']}")
    return 0


# ---------------------------------------------------------------------------
# Record — write path through axiom.memory.session_capture
# ---------------------------------------------------------------------------


def _cmd_record(args: argparse.Namespace) -> int:
    """Append a conversation turn through the cross-tool common path."""
    from axiom.memory.session_capture import record_session_turn, resolve_principal_id

    if args.json_stdin:
        try:
            event = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(
                f"axi memory record: invalid JSON on stdin: {exc}",
                file=sys.stderr,
            )
            return 1
        explicit_principal = event.get("principal_id") or event.get("principal")
        tool = event.get("tool")
        if not tool:
            print(
                "axi memory record: stdin event missing tool",
                file=sys.stderr,
            )
            return 1
        try:
            principal_id = resolve_principal_id(explicit_principal)
        except ValueError as exc:
            print(f"axi memory record: {exc}", file=sys.stderr)
            return 1
        kwargs = dict(
            principal_id=principal_id,
            tool=tool,
            user_input=event.get("user_input", ""),
            assistant_output=event.get("assistant_output", ""),
            model=event.get("model"),
            summary=event.get("summary"),
            scope=event.get("scope", "user"),
            event_time=event.get("event_time"),
            extra=event.get("extra"),
        )
        emit_json = bool(args.json) or True  # stdin path defaults to JSON output
    else:
        if not args.tool:
            print(
                "axi memory record: --tool is required (or use --json-stdin).",
                file=sys.stderr,
            )
            return 1
        try:
            principal_id = resolve_principal_id(args.principal_id)
        except ValueError as exc:
            print(f"axi memory record: {exc}", file=sys.stderr)
            return 1
        kwargs = dict(
            principal_id=principal_id,
            tool=args.tool,
            user_input=args.user_input,
            assistant_output=args.assistant_output,
            model=args.model,
            summary=args.summary,
            scope=args.scope,
        )
        emit_json = bool(args.json)

    composition = _build_default_composition()
    frag = record_session_turn(composition=composition, **kwargs)

    payload = {
        "fragment_id": frag.id,
        "principal_id": kwargs["principal_id"],
        "tool": kwargs["tool"],
        "model": kwargs.get("model") or "",
        "event_time": frag.content.get("event_time", ""),
    }

    if emit_json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"recorded fragment {frag.id} "
            f"(principal={kwargs['principal_id']}, tool={kwargs['tool']})"
        )
    return 0


# ---------------------------------------------------------------------------
# Ingest — backstop for cross-tool capture (Claude Code transcripts today)
# ---------------------------------------------------------------------------


def _cmd_ingest(args: argparse.Namespace) -> int:
    """Fold a Claude Code session transcript into the ledger.

    Single-pass mode (default) ingests once and exits. ``--watch``
    flips into polling mode that keeps ingesting as the transcript
    grows.
    """
    import os as _os

    from axiom.memory.session_capture import (
        watch_ingest_claude_code_jsonl,
    )

    composition = _build_default_composition()

    from axiom.memory.session_capture import resolve_principal_id
    try:
        principal_id = resolve_principal_id(args.principal_id)
    except ValueError as exc:
        print(f"axi memory ingest: {exc}", file=sys.stderr)
        return 1

    if args.watch:
        # In watch mode, a missing path is acceptable at start — the
        # transcript may show up later. Single-pass mode keeps the strict
        # check below.
        report = watch_ingest_claude_code_jsonl(
            composition=composition,
            path=args.path,
            principal_id=principal_id,
            interval_s=args.interval,
            max_iterations=args.max_iterations,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"# Memory ingest --watch — {args.path}")
            print()
            print(f"- Iterations: {report['iterations']}")
            print(f"- Total scanned (turn pairs): {report['total_scanned']}")
            print(f"- Total written: {report['total_written']}")
            print(f"- Total skipped: {report['total_skipped']}")
        return 0

    if not _os.path.exists(args.path):
        print(
            f"axi memory ingest: file not found: {args.path}",
            file=sys.stderr,
        )
        return 1

    from axiom.memory.session_capture import ingest_session_log

    try:
        report = ingest_session_log(
            composition=composition,
            path=args.path,
            principal_id=principal_id,
            tool=args.tool,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    except (NotImplementedError, ValueError) as exc:
        print(f"axi memory ingest: {exc}", file=sys.stderr)
        return 1
    report["path"] = args.path
    report["principal_id"] = principal_id
    report["dry_run"] = args.dry_run
    report["tool"] = args.tool

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"# Memory ingest — {args.path}")
    print()
    if args.dry_run:
        print("**Dry run** — no fragments written.")
        print()
    print(f"- Scanned (turn pairs): {report['scanned']}")
    print(f"- Written: {report['written']}")
    print(f"- Skipped (already ingested): {report['skipped']}")
    return 0


# ---------------------------------------------------------------------------
# Heartbeat — periodic write that proves the memory write path is alive
# ---------------------------------------------------------------------------


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    from axiom.memory.session_capture import (
        record_heartbeat,
        resolve_principal_id,
    )

    try:
        principal_id = resolve_principal_id(args.principal_id)
    except ValueError as exc:
        print(f"axi memory heartbeat: {exc}", file=sys.stderr)
        return 1

    composition = _build_default_composition()
    frag = record_heartbeat(
        composition=composition,
        principal_id=principal_id,
        source=args.source,
    )

    payload = {
        "fragment_id": frag.id,
        "principal_id": principal_id,
        "source": args.source,
        "event_time": frag.content.get("event_time", ""),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"heartbeat recorded: fragment={frag.id} "
            f"(principal={principal_id}, source={args.source})"
        )
    return 0


def _cmd_heartbeat_install(args: argparse.Namespace) -> int:
    from axiom.extensions.builtins.memory.heartbeat_install import (
        install_heartbeat_plist,
    )

    result = install_heartbeat_plist(
        interval_seconds=args.interval,
        load=not args.no_load,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"axi memory heartbeat-install: wrote {result['plist_path']}")
        print(f"  axi binary: {result['axi_binary']}")
        print(f"  interval:   {result['interval_seconds']}s")
        print(f"  log dir:    {result['log_dir']}")
        if not args.no_load:
            print(f"  launchctl:  {result['load_message']}")
    return 0 if (args.no_load or result.get("loaded")) else 1


def _cmd_heartbeat_uninstall(args: argparse.Namespace) -> int:
    from axiom.extensions.builtins.memory.heartbeat_install import (
        uninstall_heartbeat_plist,
    )

    result = uninstall_heartbeat_plist(unload=not args.no_unload)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result.get("removed"):
            print(f"axi memory heartbeat-uninstall: removed {result['plist_path']}")
        else:
            print(
                f"axi memory heartbeat-uninstall: nothing to remove "
                f"({result['plist_path']})"
            )
        if not args.no_unload:
            print(f"  launchctl:  {result.get('unload_message', '')}")
    return 0


# ---------------------------------------------------------------------------
# Register-MCP — write user-scope axiom-memory registration
# ---------------------------------------------------------------------------


def _cmd_register_mcp(args: argparse.Namespace) -> int:
    """Register (or check) the axiom-memory MCP user-scope entry.

    Modes:
      default            register Claude Code only (back-compat)
      --tool <name>      register a single tool by name
      --all              walk every detected tool in TOOL_REGISTRARS
      --check            detection-only exit code; respects --tool / --all
    """
    from axiom.extensions.builtins.memory.register_mcp import (
        TOOL_REGISTRARS,
        is_axiom_memory_mcp_registered,
        register_all_detected,
        register_axiom_memory_mcp,
    )

    if not args.check:
        print(
            "note: `axi memory register-mcp` is superseded by `axi mcp install`, "
            "which registers the full MCP surface (memory included) across all "
            "detected IDEs. This memory-only path still works.",
            file=sys.stderr,
        )

    # ---- --all path -----
    if args.all_tools:
        if args.check:
            statuses: dict[str, dict] = {}
            any_failed = False
            for name, reg in TOOL_REGISTRARS.items():
                if not reg.detect():
                    statuses[name] = {
                        "registered": None, "reason": "not_detected", "tool": name,
                    }
                    continue
                statuses[name] = reg.is_registered(expected_command=sys.executable)
                if not statuses[name].get("registered"):
                    any_failed = True
            if args.json:
                print(json.dumps(statuses, indent=2))
            else:
                for name, s in statuses.items():
                    if s.get("registered") is None:
                        print(f"  [skip] {name}: not detected")
                    elif s.get("registered"):
                        flag = " (stale)" if s.get("stale") else ""
                        print(f"  [ok]   {name}{flag}: command={s.get('command', '')}")
                    else:
                        print(f"  [fail] {name}: {s.get('reason', 'missing')}")
                        print(f"         fix: axi memory register-mcp --tool {name}")
            return 1 if any_failed else 0

        results = register_all_detected()
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            for name, r in results.items():
                action = r.get("action")
                if action == "skipped":
                    print(f"  [skip] {name}: {r.get('reason', '')}")
                elif action == "stub":
                    print(f"  [stub] {name}: not implemented")
                else:
                    cmd = r.get("command", "")
                    print(f"  [{action}] {name}: command={cmd}")
        return 0

    # ---- --tool <name> path -----
    if args.tool is not None:
        reg = TOOL_REGISTRARS.get(args.tool)
        if reg is None:
            known = ", ".join(sorted(TOOL_REGISTRARS))
            print(
                f"axi memory register-mcp: unknown tool '{args.tool}'. "
                f"Known: {known}",
                file=sys.stderr,
            )
            return 1

        if args.check:
            status = reg.is_registered(expected_command=sys.executable)
            if args.json:
                print(json.dumps(status, indent=2))
            else:
                if not status.get("registered"):
                    print(
                        f"axi memory register-mcp ({args.tool}): not registered "
                        f"({status.get('reason', 'unknown')})",
                        file=sys.stderr,
                    )
                    print(
                        f"  fix: axi memory register-mcp --tool {args.tool}",
                        file=sys.stderr,
                    )
                elif status.get("stale"):
                    print(
                        f"axi memory register-mcp ({args.tool}): registered "
                        f"with stale python path ({status.get('command')})",
                        file=sys.stderr,
                    )
                    print(
                        f"  fix: axi memory register-mcp --tool {args.tool}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"axi memory register-mcp ({args.tool}): ok "
                        f"(command={status.get('command')})"
                    )
            registered_ok = status.get("registered") and not status.get("stale")
            return 0 if registered_ok else 1

        try:
            result = reg.register(python_path=sys.executable)
        except NotImplementedError as exc:
            print(f"axi memory register-mcp ({args.tool}): {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            action = result["action"]
            if action == "unchanged":
                print(
                    f"axi memory register-mcp ({args.tool}): already registered "
                    f"({result['command']})"
                )
            else:
                print(
                    f"axi memory register-mcp ({args.tool}): {action} entry in "
                    f"{result['config_path']} (command={result['command']})"
                )
        return 0

    # ---- default path: Claude Code only (back-compat) -----
    if args.check:
        status = is_axiom_memory_mcp_registered(expected_command=sys.executable)
        if args.json:
            print(json.dumps(status, indent=2))
        else:
            if not status.get("registered"):
                print(
                    "axi memory register-mcp: not registered "
                    f"({status.get('reason', 'unknown')})",
                    file=sys.stderr,
                )
                print("  fix: axi memory register-mcp", file=sys.stderr)
            elif status.get("stale"):
                print(
                    "axi memory register-mcp: registered with stale python "
                    f"path ({status.get('command')})",
                    file=sys.stderr,
                )
                print("  fix: axi memory register-mcp", file=sys.stderr)
            else:
                print(
                    f"axi memory register-mcp: ok "
                    f"(command={status.get('command')})"
                )
        registered_ok = status.get("registered") and not status.get("stale")
        return 0 if registered_ok else 1

    result = register_axiom_memory_mcp()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        action = result["action"]
        if action == "unchanged":
            print(
                f"axi memory register-mcp: already registered "
                f"({result['command']})"
            )
        else:
            print(
                f"axi memory register-mcp: {action} entry in "
                f"{result['config_path']} (command={result['command']})"
            )
    return 0


# ---------------------------------------------------------------------------
# Migration helpers (ADR-035 §D7 — backfill accountable_human_id on v1)
# ---------------------------------------------------------------------------


def _looks_like_human_principal(principal_id: str) -> bool:
    """Heuristic: is this principal_id a human or an agent?

    Per CLAUDE.md the project convention is ``@name:context`` Matrix-style
    for humans. Agents use names like ``agent:axi`` or the AXI
    ALL-CAPS-HYPHEN convention. We treat anything beginning with
    ``agent:`` or matching the well-known agent ALL-CAPS pattern as an
    agent; everything else (including ``@name:context`` and email-like
    addresses) is treated as human-shaped.
    """
    if not principal_id:
        return False
    lowered = principal_id.lower()
    if lowered.startswith("agent:"):
        return False
    # Well-known platform agents from CLAUDE.md naming convention.
    KNOWN_AGENTS = {
        "axi", "scan", "tidy", "press", "triage", "curio", "chalke",
        "warden", "vega",
    }
    if lowered in KNOWN_AGENTS:
        return False
    return True


def _scope_matches(fragment_data: dict, scope_id: str) -> bool:
    """Return True iff this fragment data dict belongs to scope_id.

    Stage-3-era fragments carry scope under ``content.scope`` (preferred)
    or ``content.classroom_id`` (canary alias). See spec-memory §3 + the
    interaction-writer adapter.
    """
    content = fragment_data.get("content") or {}
    if content.get("scope") == scope_id:
        return True
    if content.get("classroom_id") == scope_id:
        return True
    return False


def backfill_accountable_human(
    composition: Any,
    *,
    scope_id: str,
    dry_run: bool = False,
    default_human: str | None = None,
) -> dict:
    """Walk v1 fragments in ``scope_id`` and migrate them to v2.

    Per ``working/memory-persistence-plan.md`` §5, migration tombstones
    the original and writes a *new* v2 fragment carrying the inferred
    accountable_human_id. The audit trail is preserved on disk: original
    fragment + tombstone + new fragment all coexist.

    Inference rules (ADR-035 §D7):

    - Human-shaped ``principal_id`` → use as ``accountable_human_id``.
      ``delegation_chain = ()`` (the human acted directly).
    - Agent-shaped ``principal_id`` and ``default_human`` provided →
      ``accountable_human_id = default_human``;
      ``delegation_chain = (default_human, principal_id)``.
    - Agent-shaped ``principal_id`` and no ``default_human`` →
      ambiguous; emit ``ProvenanceAmbiguous`` audit event; do NOT
      tombstone. Reviewer reruns with ``--default-human`` once decided.

    Returns a report dict with counts: ``scanned``, ``would_migrate``,
    ``written``, ``ambiguous``, ``skipped`` (already-v2 fragments).
    """

    report = {
        "scope": scope_id,
        "dry_run": dry_run,
        "scanned": 0,
        "would_migrate": 0,
        "written": 0,
        "ambiguous": 0,
        "skipped": 0,
    }

    artifacts = composition.artifact_registry.list(kind="fragment")

    for artifact in artifacts:
        data = artifact.data or {}
        if not _scope_matches(data, scope_id):
            continue
        report["scanned"] += 1

        version = int(data.get("schema_version", 1))
        prov = data.get("provenance") or {}
        existing_human = prov.get("accountable_human_id")

        # Already migrated — skip.
        if (
            version >= 2
            and existing_human
            and not str(existing_human).startswith("legacy:")
        ):
            report["skipped"] += 1
            continue

        principal = prov.get("principal_id", "")
        is_human = _looks_like_human_principal(principal)

        if is_human:
            new_human = principal
            new_chain: tuple[str, ...] = ()
        elif default_human is not None:
            new_human = default_human
            new_chain = (default_human, principal)
        else:
            # Ambiguous: agent-principal, no default_human.
            report["ambiguous"] += 1
            composition.audit_log.record(
                entry_type="ProvenanceAmbiguous",
                principal_id=principal,
                agent_id=principal,
                fragment_id=artifact.name,
                outcome="ambiguous_no_default_human",
                scope=scope_id,
            )
            continue

        report["would_migrate"] += 1

        if dry_run:
            continue

        # Build the v2 successor. Preserve every field; replace the
        # provenance + bump schema_version.
        v2_data = dict(data)
        v2_prov = dict(prov)
        v2_prov["accountable_human_id"] = new_human
        v2_prov["delegation_chain"] = list(new_chain)
        v2_data["provenance"] = v2_prov
        v2_data["schema_version"] = 2

        # New fragment id — the migrated fragment is a *new* event per
        # memory-persistence-plan §5 (immutable history; supersede,
        # don't overwrite). Stamp ``superseded_by`` on the audit
        # entry's content so reverse-lookup works.
        import uuid as _uuid

        new_id = _uuid.uuid4().hex
        v2_data["id"] = new_id
        # Carry forward the originating-id for audit traceability.
        v2_content = dict(v2_data.get("content") or {})
        v2_content["migrated_from"] = artifact.name
        v2_data["content"] = v2_content

        composition.artifact_registry.register(
            kind="fragment", name=new_id, data=v2_data,
        )

        # Tombstone the original.
        composition.artifact_registry.delete(
            artifact.id, reason="migrated_to_v2",
        )

        composition.audit_log.record(
            entry_type="ProvenanceMigrated",
            principal_id=principal,
            agent_id=principal,
            fragment_id=artifact.name,
            outcome="migrated_to_v2",
            scope=scope_id,
            new_fragment_id=new_id,
            accountable_human=new_human,
        )
        report["written"] += 1

    return report


def _cmd_migrate(args: argparse.Namespace) -> int:
    if not args.backfill_scope:
        print(
            "axi memory migrate: nothing to do. Pass "
            "--backfill-accountable-human <scope_id>.",
            file=sys.stderr,
        )
        return 1

    if args.classroom_id:
        composition = _build_composition_for_classroom(args.classroom_id)
    else:
        composition = _build_default_composition()

    report = backfill_accountable_human(
        composition=composition,
        scope_id=args.backfill_scope,
        dry_run=args.dry_run,
        default_human=args.default_human,
    )

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(_h1(f"Memory migration — scope={report['scope']}"))
    print()
    if report["dry_run"]:
        print(_bold("Dry run") + " — no changes written.")
        print()
    print(f"- Scanned: {report['scanned']}")
    print(f"- Already-migrated (skipped): {report['skipped']}")
    print(f"- Would migrate: {report['would_migrate']}")
    print(f"- Written: {report['written']}")
    print(f"- Ambiguous (need --default-human): {report['ambiguous']}")
    if report["ambiguous"] and not args.default_human:
        print()
        print(
            "_Re-run with `--default-human <principal_id>` to backfill "
            "ambiguous fragments, or review the audit projection for "
            "ProvenanceAmbiguous events to assign per fragment._"
        )
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _cmd_reindex(args: argparse.Namespace) -> int:
    """Thin ADR-056 wrapper: invoke memory.reindex_recall (corpus backfill)."""
    from .skills.reindex_recall import reindex_recall

    params: dict = {
        "composition": _build_default_composition(),
        "all": args.all_principals,
    }
    if not args.all_principals:
        from axiom.memory.session_capture import resolve_principal_id

        try:
            params["principal"] = resolve_principal_id(args.principal)
        except ValueError as exc:
            print(f"axi memory reindex: {exc}", file=sys.stderr)
            return 1

    result = reindex_recall(params, None)

    if args.json and result.value is not None:
        print(json.dumps(result.value, indent=2))
        return result.exit_code
    if not result.ok:
        for err in result.errors:
            print(f"axi memory reindex: {err}", file=sys.stderr)
        return result.exit_code

    v = result.value
    print(_h1("Recall corpus reindex"))
    print()
    print(f"- Principals reindexed: {len(v['principals'])}")
    print(f"- Fragments projected: {v['reindexed']}")
    for principal, count in v["per_principal"].items():
        print(f"    {principal}: {count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.action:
        parser.print_help()
        return 1

    if args.action == "show":
        return _cmd_show(args)
    if args.action == "record":
        return _cmd_record(args)
    if args.action == "forget":
        return _cmd_forget(args)
    if args.action == "export":
        return _cmd_export(args)
    if args.action == "import":
        return _cmd_import(args)
    if args.action == "absorb":
        return _cmd_absorb(args)
    if args.action == "conflicts":
        return _cmd_conflicts(args)
    if args.action == "dedup":
        return _cmd_dedup(args)
    if args.action == "ingest":
        return _cmd_ingest(args)
    if args.action == "register-mcp":
        return _cmd_register_mcp(args)
    if args.action == "heartbeat":
        return _cmd_heartbeat(args)
    if args.action == "heartbeat-install":
        return _cmd_heartbeat_install(args)
    if args.action == "heartbeat-uninstall":
        return _cmd_heartbeat_uninstall(args)
    if args.action == "migrate":
        return _cmd_migrate(args)
    if args.action == "reindex":
        return _cmd_reindex(args)

    print(f"Unknown action: {args.action}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
