# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/agents``: the agents this platform declares, and whether each is demonstrably running.

Read-only, and built from what the node already records:

* the roster is the discovered extensions' manifests (``[[extension.provides]] kind = "agent"`` and the
  ``[agent]`` lifecycle block);
* "enabled" is three real gates: the extension's own ``enabled`` flag, the master ``autonomy.enabled``
  setting (which ships OFF), and the operator's per-agent consent record;
* liveness is ``extensions/builtins/agents/liveness.agent_liveness``, the same producer the agent-activity
  surface uses, read from the dispatcher's own state directory.

Silence is never reported as health. A heartbeat inside twice the declared cadence is ``alive``; an older
one is ``stale``; an agent that should have a heartbeat and has none is ``no heartbeat recorded``; an agent
with no liveness source at all (or no declared cadence to judge by) is ``unknown``.

``list_agents`` and ``describe_agent`` are pure functions returning dataclasses, so an MCP tool or an agent
can reuse them. Nothing here is registered as an MCP tool.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ..command_registry import CliSpec, CommandContext, McpForm, command
from ..follow_ups import FollowUp, pick, remember_list, resolve_ref, result, with_json_follow_ups

log = logging.getLogger(__name__)

G_AGENT = "Agent"

ALIVE = "alive"
STALE = "stale"
NO_HEARTBEAT = "no heartbeat recorded"
UNKNOWN = "unknown"

#: A heartbeat is "alive" while it is younger than this many declared intervals. One missed beat is jitter
#: (a loaded machine, a skipped tick); two is a pattern.
ALIVE_WITHIN_INTERVALS = 2

DELEGATION_NOTICE = (
    "Handing work to an agent from chat (delegation or dispatch) is NOT available yet: the delegation "
    "controls (issue #1067) are not built, so no command here sends an agent a task. Agents run on their "
    "own schedule, or are invoked through their extension's CLI noun."
)


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001 - a label never takes the command down
        return "axi"


# ---- data ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRow:
    name: str  # tidy
    extension: str  # hygiene: the extension that owns it
    purpose: str  # one line
    principal: str  # @tidy:local
    extension_enabled: bool
    scheduled: bool  # has a heartbeat command the background service can fire
    autonomy_on: bool | None  # the master switch; None = could not be read
    consent: str  # enabled | not enabled by operator | opted out | undecided | n/a
    runs: str  # what actually happens to this agent, in words
    cadence_seconds: int | None
    last_activity: str | None  # ISO 8601, from the dispatcher's state or the agent's own ledger
    age_seconds: float | None
    liveness: str  # alive | stale | no heartbeat recorded | unknown
    liveness_detail: str
    persona: str | None = None  # path of the declared persona file, resolved
    default_consent: str | None = None  # [agent].default_consent, the declared tier


@dataclass(frozen=True)
class AgentsReport:
    rows: tuple[AgentRow, ...]
    autonomy_on: bool | None
    consent_summary: str
    delegation_available: bool = False  # always False until #1067's controls exist
    notice: str = DELEGATION_NOTICE


@dataclass(frozen=True)
class HeldAction:
    action_id: str
    name: str
    created_at: str
    reason: str = ""


@dataclass(frozen=True)
class AgentDetail:
    row: AgentRow
    persona_summary: str
    skills: tuple[str, ...]  # capabilities the agent's extension owns
    allowed_tools: tuple[str, ...]  # tools those capabilities declare they may invoke
    raci: dict[str, Any] = field(default_factory=dict)  # declared tier + recorded approval states
    held: tuple[HeldAction, ...] = ()
    held_note: str = ""  # why held is empty or unknown


# ---- reading the node ---------------------------------------------------------------------------


def _agents_dir() -> Path:
    """The dispatcher's own state directory, so liveness is read from where the dispatcher writes."""
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "agents"


def _autonomy() -> bool | None:
    try:
        from axiom.extensions.builtins.settings.store import autonomy_enabled

        return bool(autonomy_enabled())
    except Exception:  # noqa: BLE001
        return None


def _consent():
    try:
        from axiom.extensions.builtins.agents.consent import load_consent

        return load_consent()
    except Exception:  # noqa: BLE001
        return None


def _consent_for(ext_name: str, consent) -> str:
    if consent is None:
        return "unknown"
    if consent.opted_out:
        return "opted out"
    if not consent.decided:
        return "undecided"
    return "enabled" if ext_name in consent.enabled else "not enabled by operator"


def _consent_summary(consent) -> str:
    if consent is None:
        return "agent consent record unreadable"
    if consent.opted_out:
        return "operator opted out of all agents"
    if not consent.decided:
        return "no consent decision recorded: every scheduled agent is dispatched (once autonomy is on)"
    return f"operator enabled {len(consent.enabled)} agent(s): {', '.join(sorted(consent.enabled)) or 'none'}"


def _runs(*, ext_enabled: bool, scheduled: bool, autonomy: bool | None, consent: str) -> str:
    cli = _brand_cli()
    if not ext_enabled:
        return "extension disabled"
    if not scheduled:
        return "on demand (no scheduled heartbeat declared)"
    if autonomy is None:
        return "unknown (autonomy setting unreadable)"
    if not autonomy:
        return f"OFF: autonomy.enabled is false, nothing is dispatched ({cli} settings set autonomy.enabled true)"
    if consent == "opted out":
        return "OFF: operator opted out of all agents"
    if consent == "not enabled by operator":
        return "OFF: not in the operator's enabled agents"
    return "scheduled"


def _principal_of(ext, raw: dict) -> str:
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    display = str(sender.get("display_name") or "").strip().lower()
    return display or ext.name


def _declared_interval(agent_table: dict) -> int | None:
    """``heartbeat_interval`` only when the manifest actually contains the key.

    The parsed ``AgentConfig`` defaults it to 300, which would invent a declaration for every agent that
    never made one (the same trap ``agents/liveness`` documents); the raw table's key PRESENCE decides.
    """
    value = agent_table.get("heartbeat_interval")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _classify(
    *, scheduled_block: bool, status: dict | None, cadence: int | None, now: float
) -> tuple[str, str, float | None, str | None, int | None]:
    """``(liveness, detail, age_seconds, last_activity_iso, cadence)``. Silence is never ``alive``."""
    from datetime import datetime

    last = (status or {}).get("lastReportAt")
    age = None
    if last:
        try:
            age = max(0.0, now - datetime.fromisoformat(str(last)).timestamp())
        except ValueError:
            last = None
    if not last:
        if not scheduled_block:
            return UNKNOWN, "no liveness source: it is not scheduled (no heartbeat command) and keeps no ledger", None, None, None
        return (
            NO_HEARTBEAT,
            "neither the background service's state nor an agent ledger holds a record for it",
            None, None, cadence,
        )
    if not isinstance(cadence, int) or cadence <= 0:
        return (
            UNKNOWN,
            "it has reported, but its manifest declares no heartbeat_interval, so staleness cannot be judged",
            age, str(last), None,
        )
    if age <= ALIVE_WITHIN_INTERVALS * cadence:
        return ALIVE, f"last report within {ALIVE_WITHIN_INTERVALS}x its {cadence}s interval", age, str(last), cadence
    return (
        STALE,
        f"last report is older than {ALIVE_WITHIN_INTERVALS}x its {cadence}s interval",
        age, str(last), cadence,
    )


def _load_raw(ext) -> dict:
    from axiom.infra.toml_compat import load_toml

    path = getattr(ext, "manifest_path", None)
    if path is None:
        return {}
    try:
        return load_toml(path) or {}
    except Exception:  # noqa: BLE001
        return {}


def _agent_provides(raw: dict) -> list[dict]:
    provides = (raw.get("extension") or {}).get("provides") or []
    return [p for p in provides if isinstance(p, dict) and p.get("kind") == "agent" and p.get("name")]


def _liveness_by_principal(agents_dir: Path) -> dict[str, dict]:
    try:
        from axiom.extensions.builtins.agents.liveness import agent_liveness

        rows = agent_liveness(agents_dir=agents_dir)
    except Exception as exc:  # noqa: BLE001 - unreadable liveness is unknown, never healthy
        log.debug("agents: liveness unavailable: %s", exc)
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        handle = str(r.get("agent", ""))
        out[handle[1:].split(":", 1)[0]] = r
    return out


def list_agents(
    *,
    extensions: list | None = None,
    agents_dir: Path | None = None,
    now: float | None = None,
    autonomy: bool | None = ...,  # type: ignore[assignment]
    consent: Any = ...,
) -> AgentsReport:
    """Every declared agent with its liveness. Arguments let a caller supply the node's facts."""
    if extensions is None:
        from axiom.extensions.discovery import surfaced_extensions

        extensions = surfaced_extensions()
    now = time.time() if now is None else now
    autonomy_on = _autonomy() if autonomy is ... else autonomy
    consent_rec = _consent() if consent is ... else consent
    liveness = _liveness_by_principal(Path(agents_dir) if agents_dir else _agents_dir())

    rows: list[AgentRow] = []
    for ext in sorted(extensions, key=lambda e: e.name):
        raw = _load_raw(ext)
        provides = _agent_provides(raw)
        agent_cfg = getattr(ext, "agent", None)
        if not provides and agent_cfg is None:
            continue
        principal = _principal_of(ext, raw)
        agent_table = raw.get("agent") if isinstance(raw.get("agent"), dict) else {}
        scheduled = bool(agent_cfg is not None and getattr(agent_cfg, "is_registrable", False))
        consent_state = _consent_for(ext.name, consent_rec) if scheduled else "n/a"
        entries = provides or [{"name": ext.name, "description": getattr(ext, "description", "")}]
        # The lifecycle block and its liveness belong to the extension. Attach it to the agent whose name is
        # the extension's principal (or its only agent); any sibling declared by the same extension has no
        # liveness source of its own and says so.
        owner = next((e for e in entries if str(e["name"]).lower() == principal), entries[0])
        for entry in entries:
            is_owner = entry is owner
            state, detail, age, last, cadence = _classify(
                scheduled_block=is_owner and scheduled,
                status=(liveness.get(principal) or liveness.get(ext.name)) if is_owner else None,
                cadence=_declared_interval(agent_table) if is_owner else None,
                now=now,
            )
            persona = entry.get("persona")
            rows.append(
                AgentRow(
                    name=str(entry["name"]),
                    extension=ext.name,
                    purpose=_one_line(str(entry.get("description") or getattr(ext, "description", "") or "")),
                    principal=f"@{principal if is_owner else entry['name']}",
                    extension_enabled=bool(getattr(ext, "enabled", True)),
                    scheduled=scheduled and is_owner,
                    autonomy_on=autonomy_on,
                    consent=consent_state if is_owner else "n/a",
                    runs=_runs(
                        ext_enabled=bool(getattr(ext, "enabled", True)),
                        scheduled=scheduled and is_owner,
                        autonomy=autonomy_on,
                        consent=consent_state,
                    ),
                    cadence_seconds=cadence,
                    last_activity=last,
                    age_seconds=age,
                    liveness=state,
                    liveness_detail=detail,
                    persona=str(Path(ext.root) / persona) if persona and getattr(ext, "root", None) else None,
                    default_consent=agent_table.get("default_consent") if is_owner else None,
                )
            )
    rows.sort(key=lambda r: r.name.lower())
    return AgentsReport(rows=tuple(rows), autonomy_on=autonomy_on, consent_summary=_consent_summary(consent_rec))


def filter_agents(report: AgentsReport, needle: str) -> AgentsReport:
    n = needle.strip().lower()
    if not n:
        return report
    rows = tuple(r for r in report.rows if n in r.name.lower() or n in r.extension.lower() or n in r.purpose.lower())
    return replace(report, rows=rows)


def _one_line(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= 140 else text[:137].rstrip() + "..."


# ---- detail -------------------------------------------------------------------------------------


def persona_summary(path: str | None) -> str:
    """The persona's title and first prose lines. Never raises; says so when there is nothing to read."""
    if not path:
        return "no persona file declared"
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return f"persona file declared but not readable: {path}"
    title = ""
    prose: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if not title:
                title = s.lstrip("# ").strip()
            elif prose:
                break
            continue
        prose.append(s)
        if len(" ".join(prose)) > 320:
            break
    body = " ".join(prose)
    body = body if len(body) <= 360 else body[:357].rstrip() + "..."
    return (f"{title}: {body}" if title and body else title or body) or "persona file is empty"


def _owned_skills(ext, registry) -> tuple[list[str], list[str]]:
    """Capabilities defined under the agent's extension package, and the tools they declare they may use."""
    from axiom.extensions.discovery import _extension_source_package

    pkg = _extension_source_package(ext)
    if not pkg or registry is None:
        return [], []
    prefix = f"{pkg}.extensions.builtins.{ext.name}."
    names, tools_ = [], set()
    for name, spec in sorted(registry.specs().items()):
        if str(getattr(spec.fn, "__module__", "")).startswith(prefix):
            names.append(name)
            tools_.update(spec.allowed_tools or ())
    return names, sorted(tools_)


def _raci(ext, principal: str, default_consent: str | None, state_dir: Path | None) -> dict[str, Any]:
    from axiom.agents.raci import RACILedger

    out: dict[str, Any] = {"declared_default_consent": default_consent, "ledger": None}
    base = (state_dir or _agents_dir())
    for key in dict.fromkeys((principal, ext.name)):
        path = base / key / "raci_state.json"
        if not path.exists():
            continue
        try:
            ledger = RACILedger.load(path)
        except Exception as exc:  # noqa: BLE001
            out["ledger"] = f"unreadable: {exc}"
            return out
        out["ledger"] = {name: a.state.value for name, a in sorted(ledger.actions.items())}
        out["ledger_path"] = str(path)
        return out
    return out


def _held_for(names: list[str], approvals_path: Path | None) -> tuple[tuple[HeldAction, ...], str]:
    from axiom.infra.orchestrator.actions import ActionStatus
    from axiom.infra.orchestrator.approval_store import FileActionStore

    try:
        actions = FileActionStore(approvals_path).all()
    except Exception as exc:  # noqa: BLE001 - an unreadable queue is unknown, never "nothing held"
        return (), f"unknown: the approval queue could not be read ({exc})"
    mine = tuple(
        HeldAction(a.action_id, a.name, a.created_at, getattr(a, "reason", "") or "")
        for a in actions
        if a.status is ActionStatus.PENDING and a.name in set(names)
    )
    if not names:
        return mine, "none attributable: this agent's extension owns no registered skills"
    return mine, ""


def describe_agent(
    name: str,
    *,
    report: AgentsReport | None = None,
    extensions: list | None = None,
    catalog=None,
    agents_dir: Path | None = None,
    approvals_path: Path | None = None,
    **kwargs: Any,
) -> AgentDetail | None:
    """Everything known about one agent, or None when no agent has that name."""
    if extensions is None:
        from axiom.extensions.discovery import surfaced_extensions

        extensions = surfaced_extensions()
    report = report or list_agents(extensions=extensions, agents_dir=agents_dir, **kwargs)
    row = next((r for r in report.rows if r.name == name), None)
    if row is None:
        return None
    ext = next(e for e in extensions if e.name == row.extension)
    if catalog is None:
        from .skills_cmd import discover_catalog

        catalog = discover_catalog()
    skills, allowed = _owned_skills(ext, catalog)
    held, note = _held_for(skills, approvals_path)
    return AgentDetail(
        row=row,
        persona_summary=persona_summary(row.persona),
        skills=tuple(skills),
        allowed_tools=tuple(allowed),
        raci=_raci(ext, row.principal.lstrip("@"), row.default_consent, agents_dir),
        held=held,
        held_note=note,
    )


# ---- rendering ----------------------------------------------------------------------------------


def _age(row: AgentRow) -> str:
    from axiom.infra.cli_format import relative_age

    return relative_age(row.last_activity) if row.last_activity else "never"


FOLLOW_UPS = {
    "open": FollowUp("/agents 1", "open the first agent: persona, skills, approval posture, held actions"),
    "detail": FollowUp("/agents {name}", "persona, skills, approval posture and held actions"),
    "autonomy": FollowUp(
        "{cli} settings set autonomy.enabled true",
        "autonomy is off, so no agent is dispatched; this is the master switch",
    ),
    "held": FollowUp("/permissions held", "actions held for a human, with ids and how to approve"),
    "skills": FollowUp("/skills", "the capabilities an agent's extension registers"),
    "all": FollowUp("/agents", "list every agent again"),
}


def listing_keys(report: AgentsReport) -> list:
    keys: list = []
    if report.autonomy_on is False:
        keys.append("autonomy")
    keys.append("open" if report.rows else "all")
    return keys


def detail_keys(d: AgentDetail) -> list:
    keys: list = []
    if d.row.autonomy_on is False:
        keys.append("autonomy")
    if d.held:
        keys.append("held")
    keys += ["skills", "all"]
    return keys


def format_listing(report: AgentsReport, needle: str = "") -> str:
    from axiom.infra.cli_format import Column, table

    rows = report.rows
    if needle:
        rows = filter_agents(report, needle).rows
        if not rows:
            return f"\n  No agent matches {needle!r}. `/agents` lists them all.\n"
    auto = {True: "ON", False: "OFF", None: "unknown"}[report.autonomy_on]
    lines = ["", f"  {len(rows)} declared agent{'s' if len(rows) != 1 else ''}   autonomy: {auto}   {report.consent_summary}", ""]
    lines.extend(
        table(
            [
                (str(i), r.name, r.extension, _short_runs(r), _age(r), r.liveness, r.purpose)
                for i, r in enumerate(rows, 1)
            ],
            [
                Column("#", align="right"),
                Column("agent"),
                Column("extension"),
                Column("runs"),
                Column("last activity"),
                Column("liveness"),
                Column("purpose", wrap=True),
            ],
            width=120,
            indent=4,
            headers=True,
        )
    )
    lines.append("")
    lines.append("  liveness: alive = a report within 2x its declared interval; stale = older; no heartbeat recorded =")
    lines.append("  it should report and nothing says it ever did; unknown = no liveness source or no declared interval.")
    lines.append("  Silence is never reported as healthy. `/agents <name>` shows persona, skills, RACI and held actions (or its number: `/agents 2`).")
    lines.append(f"  {report.notice}")
    lines.append("")
    return "\n".join(lines)


def _short_runs(r: AgentRow) -> str:
    return r.runs.split(" (", 1)[0].split(":", 1)[0]


def format_detail(d: AgentDetail, report: AgentsReport) -> str:
    r = d.row
    lines = ["", f"  {r.name}   {r.principal}", f"    extension   {r.extension}", f"    purpose     {r.purpose}"]
    lines.append(f"    runs        {r.runs}")
    lines.append(f"    consent     {r.consent}")
    lines.append(f"    liveness    {r.liveness}: {r.liveness_detail}")
    lines.append(f"    last seen   {_age(r)}" + (f"  ({r.last_activity})" if r.last_activity else ""))
    if r.cadence_seconds:
        lines.append(f"    interval    {r.cadence_seconds}s (declared)")
    lines += ["", "  Persona", f"    {d.persona_summary}"]
    if r.persona:
        lines.append(f"    file: {r.persona}")
    lines += ["", f"  Skills it may use ({len(d.skills)}, the capabilities its extension registers)"]
    lines += [f"    {s}" for s in d.skills] or ["    none registered by this extension"]
    if d.allowed_tools:
        lines.append(f"    tools those skills may invoke: {', '.join(d.allowed_tools)}")
    lines += ["", "  Approval posture (RACI)"]
    lines.append(f"    declared default_consent: {d.raci.get('declared_default_consent') or 'not declared'}")
    ledger = d.raci.get("ledger")
    if isinstance(ledger, dict):
        for action_class, state in ledger.items():
            lines.append(f"    {action_class}: {state}")
        if not ledger:
            lines.append("    ledger is empty: it has proposed nothing yet")
    elif ledger:
        lines.append(f"    ledger {ledger}")
    else:
        lines.append("    no RACI ledger recorded for this agent")
    lines += ["", "  Held actions (pending approval, attributed by the extension that owns the skill)"]
    if d.held:
        lines += [f"    {h.action_id}  {h.name}  created {h.created_at}" for h in d.held]
        lines.append(f"    Answer with: {_brand_cli()} approve ok <id>  or  {_brand_cli()} approve no <id> --reason ...")
    else:
        lines.append(f"    {d.held_note or 'none held'}")
    lines += ["", f"  {report.notice}", ""]
    return "\n".join(lines)


def to_json(obj) -> str:
    return json.dumps(asdict(obj), indent=2, default=str)


# ---- completion and command ---------------------------------------------------------------------


def complete_agents(agent, args_so_far: list[str]) -> list[tuple[str, str]]:
    if args_so_far:
        return []
    try:
        report = list_agents()
    except Exception:  # noqa: BLE001 - completion never raises
        return [("--json", "machine-readable output")]
    return [(r.name, r.purpose) for r in report.rows] + [("--json", "machine-readable output")]


def agents_data(needle: str = "") -> dict:
    """``/agents --json``, ``axi chat --agents --json`` and the ``axiom_chat__agents`` tool all return this."""
    report = filter_agents(list_agents(), needle)
    payload = json.loads(to_json(report))
    payload["rows"] = [dict(r, number=i) for i, r in enumerate(payload["rows"], 1)]
    return payload


@command(
    "/agents",
    "List the platform's declared agents: what they do, whether they run, and whether they are alive",
    G_AGENT,
    usage="/agents [filter|name|number] [--json]",
    complete=complete_agents,
    follow_ups=FOLLOW_UPS,
    cli=CliSpec(("--agents",), line="/agents", applies="headless", json=True,
                help="print the declared agents and whether they are alive, and exit"),
    expose=frozenset({"chat", "cli", "mcp"}),
    mcp=(McpForm("chat.agents", "/agents"),),
)
def _agents(ctx: CommandContext, args: list[str]):
    as_json = "--json" in args
    words = [a for a in args if not a.startswith("--")]
    needle = " ".join(words).strip()
    report = list_agents()
    if len(words) == 1 and words[0].isdigit():
        key, why = resolve_ref(ctx.agent, "agents", words[0], fallback=lambda: [r.name for r in report.rows])
        if key is None:
            return f"\n  {why}\n"
        needle = key
    if needle and any(r.name == needle for r in report.rows):
        detail = describe_agent(needle, report=report)
        keys = detail_keys(detail)
        if as_json:
            return json.dumps(with_json_follow_ups(json.loads(to_json(detail)), pick("/agents", *keys)), indent=2, default=str)
        remember_list(ctx.agent, "agents", [needle])
        return result("/agents", format_detail(detail, report), *keys)
    report = filter_agents(report, needle)
    keys = listing_keys(report)
    if as_json:
        return json.dumps(with_json_follow_ups(agents_data(needle), pick("/agents", *keys)), indent=2, default=str)
    remember_list(ctx.agent, "agents", [r.name for r in report.rows])
    return result("/agents", format_listing(report, needle), *keys)
