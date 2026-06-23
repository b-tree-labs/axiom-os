# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi plan`` CLI subcommand tree — analysis §10.1.

Memory is canonical; the ``.md`` is a render artifact OR an explicit-import
draft. Every path into memory goes through a parser command. There is NO
file watcher and NO automatic sync.

Commands::

    axi plan new      --goal "<text>" --scope <id> [...flags]
    axi plan show     <plan_id> [--md-out <path>]
    axi plan edit     <plan_id>
    axi plan import   <path.md> [--scope <id>]
    axi plan refresh  <plan_id>
    axi plan approve  <plan_id> --rationale "<text>"
    axi plan reject   <plan_id> --rationale "<text>"
    axi plan list     [--scope <id>] [--status <status>]

This module ships a ``PlanStore`` Protocol so the persistence layer (task #76)
can plug in the concrete implementation in a single line. The default
``main()`` constructs an ephemeral in-memory store today; integration with
CompositionService follows once #76 lands.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from axiom.agents.pipeline.gates import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalOutcome,
    RaciAssignment,
)
from axiom.agents.pipeline.plan import (
    Plan,
    PlanRequest,
    PlanStatus,
    PlanStep,
)
from axiom.agents.pipeline.sandbox import summarize_reach
from axiom.vega.federation.policy import (
    ClassificationStamp,
    VisibilityHorizon,
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PlanCliError(Exception):
    """Base class for plan-CLI errors. Carries an exit code."""

    exit_code: int = 1


class PlanNotFound(PlanCliError):
    def __init__(self, plan_id: str) -> None:
        super().__init__(f"plan not found: {plan_id}")
        self.plan_id = plan_id
        self.exit_code = 2


class PlanParseError(PlanCliError):
    """Raised when a markdown body cannot be parsed into PlanSteps."""

    exit_code = 3


# ---------------------------------------------------------------------------
# PlanStore protocol — persistence task (#76) lands the concrete impl
# ---------------------------------------------------------------------------


class PlanStore(Protocol):
    def save(self, plan: Plan, *, raci: RaciAssignment | None = None) -> None: ...
    def load(self, plan_id: str) -> Plan: ...
    def raci_for(self, plan_id: str) -> RaciAssignment: ...
    def list_plans(
        self,
        *,
        scope_id: str | None = None,
        status: PlanStatus | None = None,
    ) -> Iterable[Plan]: ...


class _InMemoryPlanStore:
    """Default store used by main() until task #76 lands persistence.

    Process-local; vanishes between invocations. Useful for smoke tests of
    the wired-in CLI but not durable. Tests use their own stub.
    """

    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}
        self._raci: dict[str, RaciAssignment] = {}

    def save(self, plan: Plan, *, raci: RaciAssignment | None = None) -> None:
        self._plans[plan.plan_id] = plan
        if raci is not None:
            self._raci[plan.plan_id] = raci

    def load(self, plan_id: str) -> Plan:
        try:
            return self._plans[plan_id]
        except KeyError as exc:
            raise PlanNotFound(plan_id) from exc

    def raci_for(self, plan_id: str) -> RaciAssignment:
        if plan_id in self._raci:
            return self._raci[plan_id]
        plan = self.load(plan_id)
        return RaciAssignment(
            responsible=(plan.request.accountable_human_id,),
            accountable=(plan.request.accountable_human_id,),
        )

    def list_plans(
        self,
        *,
        scope_id: str | None = None,
        status: PlanStatus | None = None,
    ) -> Iterable[Plan]:
        for plan in self._plans.values():
            if scope_id and plan.request.scope_id != scope_id:
                continue
            if status and plan.status != status:
                continue
            yield plan


# ---------------------------------------------------------------------------
# Dependency bundle (testing seam)
# ---------------------------------------------------------------------------


DeriveFn = Callable[[PlanRequest], Plan]
EditorRunner = Callable[[Path], None]


def _default_editor_runner(path: Path) -> None:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    subprocess.call([editor, str(path)])


@dataclass
class PlanCliDeps:
    """Injectable dependencies for the plan CLI.

    All flows take a deps bundle so tests can swap the persistence layer,
    derive pipeline, editor invocation, and current-user lookup. The default
    factory in ``main()`` constructs production-ish deps.
    """

    store: PlanStore
    gate: ApprovalGate
    derive_fn: DeriveFn
    editor_runner: EditorRunner = field(default=_default_editor_runner)
    current_user: str = "@local:cohort.local"


# ---------------------------------------------------------------------------
# Frontmatter + body parsing
# ---------------------------------------------------------------------------


_FRONTMATTER_DELIM = "---\n"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter dict, body) from a markdown string.

    Tolerant of missing frontmatter — returns an empty dict + the original
    text unchanged. Only top-level scalar keys are extracted; multi-line
    `note:` blocks are passed through as a single joined string but never
    parsed semantically.
    """
    if not text.startswith(_FRONTMATTER_DELIM):
        return {}, text
    rest = text[len(_FRONTMATTER_DELIM) :]
    end = rest.find("\n" + _FRONTMATTER_DELIM)
    if end < 0:
        # Try `\n---` at EOF
        if rest.endswith("---\n") or rest.endswith("---"):
            block = rest.rsplit("---", 1)[0]
            return _kv_from_block(block), ""
        return {}, text
    block = rest[:end]
    body = rest[end + len("\n" + _FRONTMATTER_DELIM) :]
    return _kv_from_block(block), body


def _kv_from_block(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current_key: str | None = None
    multiline_lines: list[str] = []
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        # End any multiline accumulator if a new top-level key starts.
        if current_key and line.startswith(" "):
            multiline_lines.append(line.strip())
            continue
        if current_key:
            out[current_key] = "\n".join(multiline_lines).strip()
            current_key = None
            multiline_lines = []
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "|":
            current_key = key
            continue
        out[key] = value
    if current_key:
        out[current_key] = "\n".join(multiline_lines).strip()
    return out


def _parse_body(body: str) -> tuple[str, tuple[PlanStep, ...]]:
    """Parse a rendered/edited plan body into (goal, steps).

    Format::

        # Plan: <goal>
        ...
        ## Steps
        - intent: <text>
          tool_id: <id>
        - intent: <text>

    Tolerant of extra prose between sections. If no goal heading is found,
    returns an empty string. Raises PlanParseError if no steps section
    exists (the file is not a plan).
    """
    goal = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# Plan:"):
            goal = stripped[len("# Plan:") :].strip()
            break
        if stripped.startswith("# "):
            goal = stripped[2:].strip()
            break

    # Locate the steps section.
    steps_block: list[str] = []
    in_steps = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Steps"):
            in_steps = True
            continue
        if in_steps and stripped.startswith("## "):
            break
        if in_steps:
            steps_block.append(line)

    if not in_steps:
        raise PlanParseError("missing '## Steps' section in plan body")

    steps = tuple(_parse_steps_lines(steps_block))
    return goal, steps


def _parse_steps_lines(lines: Sequence[str]) -> Iterable[PlanStep]:
    current: dict[str, str] = {}
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            if current:
                yield _step_from_dict(current)
                current = {}
            inner = line.lstrip()[2:]
            _absorb_kv(inner, current)
        elif line.startswith(" ") and ":" in line:
            _absorb_kv(line.strip(), current)
    if current:
        yield _step_from_dict(current)


def _absorb_kv(text: str, into: dict[str, str]) -> None:
    if ":" not in text:
        return
    key, _, value = text.partition(":")
    into[key.strip()] = value.strip()


def _step_from_dict(record: dict[str, str]) -> PlanStep:
    intent = record.get("intent", "").strip()
    if not intent:
        raise PlanParseError(f"step record missing intent: {record!r}")
    tool_id = record.get("tool_id") or None
    return PlanStep(intent=intent, tool_id=tool_id)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_frontmatter(plan: Plan, *, rendered_at: str | None = None) -> str:
    rendered_at = rendered_at or _now_iso()
    return (
        "---\n"
        f"plan_id: {plan.plan_id}\n"
        f"rendered_from: {plan.plan_id}\n"
        f"rendered_at: {rendered_at}\n"
        f"status: {plan.status.value}\n"
        "note: |\n"
        "  This file is rendered from memory. Edits will be discarded unless re-imported\n"
        "  via `axi plan import` or `axi plan edit`. Memory is canonical.\n"
        "---\n"
    )


def _render_body(plan: Plan) -> str:
    lines = [f"# Plan: {plan.request.goal}", ""]
    lines.append(f"_scope_: {plan.request.scope_id}")
    lines.append(f"_principal_: {plan.request.principal_id}")
    lines.append(f"_accountable_: {plan.request.accountable_human_id}")
    lines.append("")
    lines.append("## Steps")
    lines.append("")
    if not plan.steps:
        lines.append("_(no steps)_")
    for step in plan.steps:
        lines.append(f"- intent: {step.intent}")
        if step.tool_id:
            lines.append(f"  tool_id: {step.tool_id}")
        lines.append(f"  reach: {summarize_reach(step.reach)}")
        lines.append(f"  gate: {step.gate.value}")
    lines.append("")
    return "\n".join(lines)


def _render_md(plan: Plan) -> str:
    return _render_frontmatter(plan) + _render_body(plan)


def _print_console(plan: Plan) -> None:
    """Render a plan to stdout. Rich tree if available; plain text otherwise.

    Per project memory `feedback_rich_console_lazy_construction`: build
    rich.Console() per call so capsys can intercept the output.
    """
    try:
        from rich.console import Console
        from rich.tree import Tree

        console = Console()
        tree = Tree(f"[bold]Plan[/bold]: {plan.request.goal}  ({plan.plan_id})")
        tree.add(f"scope: {plan.request.scope_id}")
        tree.add(f"status: {plan.status.value}")
        tree.add(f"principal: {plan.request.principal_id}")
        tree.add(f"accountable: {plan.request.accountable_human_id}")
        steps_node = tree.add("steps")
        for step in plan.steps:
            label = f"[cyan]{step.intent}[/cyan]"
            if step.tool_id:
                label += f"  [dim]({step.tool_id})[/dim]"
            sub = steps_node.add(label)
            sub.add(f"reach: {summarize_reach(step.reach)}")
            sub.add(f"gate: {step.gate.value}")
        console.print(tree)
    except ImportError:
        # Plain-text fallback.
        print(f"Plan: {plan.request.goal}  ({plan.plan_id})")
        print(f"  scope: {plan.request.scope_id}")
        print(f"  status: {plan.status.value}")
        print(f"  principal: {plan.request.principal_id}")
        print(f"  accountable: {plan.request.accountable_human_id}")
        print("  steps:")
        for step in plan.steps:
            tail = f"  ({step.tool_id})" if step.tool_id else ""
            print(f"    - {step.intent}{tail}")
            print(f"        reach: {summarize_reach(step.reach)}")
            print(f"        gate: {step.gate.value}")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _default_md_path(plan_id: str) -> Path:
    """Return cwd-relative ``plans/<plan_id>.md``; auto-create dir."""
    plans_dir = Path.cwd() / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    return plans_dir / f"{plan_id}.md"


def _write_md(plan: Plan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_md(plan), encoding="utf-8")


# ---------------------------------------------------------------------------
# Subcommand parsers
# ---------------------------------------------------------------------------


def _add_new_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser("new", help="Derive a new plan from a goal.")
    p.add_argument("--goal", required=True, help="Plan goal (free text).")
    p.add_argument("--scope", required=True, help="Scope id (cohort/scope).")
    p.add_argument("--principal", default=None, help="Principal (defaults to current user).")
    p.add_argument("--accountable", default=None, help="Accountable human (defaults to principal).")
    p.add_argument(
        "--target-classification",
        default="unclassified",
        help="Target classification level (default: unclassified).",
    )
    p.add_argument(
        "--target-horizon",
        default="scope_internal",
        help="Visibility horizon (default: scope_internal).",
    )
    return p


def _add_show_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser("show", help="Render a plan to console + write .md artifact.")
    p.add_argument("plan_id")
    p.add_argument("--md-out", default=None, help="Output path for .md (default plans/<id>.md).")
    return p


def _add_edit_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser("edit", help="Edit a plan via $EDITOR; saves a new version.")
    p.add_argument("plan_id")
    return p


def _add_import_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser("import", help="Parse a .md and write a new plan to memory.")
    p.add_argument("path")
    p.add_argument("--scope", default=None, help="Scope id (default: from frontmatter or 'default').")
    p.add_argument("--goal", default=None, help="Override goal (default: from body heading).")
    p.add_argument("--principal", default=None, help="Principal (defaults to current user).")
    p.add_argument("--accountable", default=None, help="Accountable human.")
    return p


def _add_refresh_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser("refresh", help="Re-render the .md artifact for a plan.")
    p.add_argument("plan_id")
    p.add_argument("--md-out", default=None, help="Output path (default plans/<id>.md).")
    return p


def _add_approve_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser("approve", help="Approve a plan (records decision).")
    p.add_argument("plan_id")
    p.add_argument("--rationale", required=True)
    return p


def _add_reject_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser("reject", help="Reject a plan (records decision).")
    p.add_argument("plan_id")
    p.add_argument("--rationale", required=True)
    return p


def _add_list_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser("list", help="List plans (filterable by scope/status).")
    p.add_argument("--scope", default=None)
    p.add_argument("--status", default=None)
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi plan",
        description="Plan I/O — create, show, edit, import, approve plans.",
    )
    sub = parser.add_subparsers(dest="action")
    _add_new_parser(sub)
    _add_show_parser(sub)
    _add_edit_parser(sub)
    _add_import_parser(sub)
    _add_refresh_parser(sub)
    _add_approve_parser(sub)
    _add_reject_parser(sub)
    _add_list_parser(sub)
    return parser


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _resolve_horizon(value: str) -> VisibilityHorizon:
    for member in VisibilityHorizon:
        if member.value == value:
            return member
    return VisibilityHorizon.SCOPE_INTERNAL


def _resolve_status(value: str) -> PlanStatus | None:
    for member in PlanStatus:
        if member.value == value:
            return member
    return None


def cmd_new(argv: Sequence[str], deps: PlanCliDeps) -> int:
    parser = argparse.ArgumentParser(prog="axi plan new")
    _populate_new_args(parser)
    args = parser.parse_args(list(argv))

    principal = args.principal or deps.current_user
    accountable = args.accountable or principal
    request = PlanRequest(
        goal=args.goal,
        scope_id=args.scope,
        principal_id=principal,
        accountable_human_id=accountable,
        target_classification=ClassificationStamp.unclassified(),
        target_horizon=_resolve_horizon(args.target_horizon),
    )
    plan = deps.derive_fn(request)
    raci = RaciAssignment(
        responsible=(accountable,),
        accountable=(accountable,),
    )
    deps.store.save(plan, raci=raci)
    md_path = _default_md_path(plan.plan_id)
    _write_md(plan, md_path)
    print(f"plan_id: {plan.plan_id}")
    _print_console(plan)
    print(f"rendered: {md_path}")
    return 0


def _populate_new_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--goal", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--principal", default=None)
    p.add_argument("--accountable", default=None)
    p.add_argument("--target-classification", default="unclassified")
    p.add_argument("--target-horizon", default="scope_internal")


def cmd_show(argv: Sequence[str], deps: PlanCliDeps) -> int:
    parser = argparse.ArgumentParser(prog="axi plan show")
    parser.add_argument("plan_id")
    parser.add_argument("--md-out", default=None)
    args = parser.parse_args(list(argv))

    try:
        plan = deps.store.load(args.plan_id)
    except PlanNotFound as exc:
        print(f"axi plan: {exc}", file=sys.stderr)
        return exc.exit_code

    _print_console(plan)
    md_path = Path(args.md_out) if args.md_out else _default_md_path(plan.plan_id)
    _write_md(plan, md_path)
    print(f"rendered: {md_path}")
    return 0


def cmd_edit(argv: Sequence[str], deps: PlanCliDeps) -> int:
    parser = argparse.ArgumentParser(prog="axi plan edit")
    parser.add_argument("plan_id")
    args = parser.parse_args(list(argv))

    try:
        plan = deps.store.load(args.plan_id)
    except PlanNotFound as exc:
        print(f"axi plan: {exc}", file=sys.stderr)
        return exc.exit_code

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(_render_md(plan))
        tmp_path = Path(tmp.name)

    try:
        deps.editor_runner(tmp_path)
        text = tmp_path.read_text(encoding="utf-8")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

    _, body = _parse_frontmatter(text)
    try:
        goal, steps = _parse_body(body)
    except PlanParseError as exc:
        print(f"axi plan edit: parse failed: {exc}", file=sys.stderr)
        return exc.exit_code

    new_request = replace(plan.request, goal=goal or plan.request.goal)
    new_plan = Plan(
        request=new_request,
        steps=steps,
        supersedes=plan.plan_id,
        derived_from=plan.derived_from,
    )
    deps.store.save(new_plan, raci=deps.store.raci_for(plan.plan_id))

    md_path = _default_md_path(new_plan.plan_id)
    _write_md(new_plan, md_path)
    print(f"plan_id: {new_plan.plan_id}")
    print(f"supersedes: {plan.plan_id}")
    print(f"rendered: {md_path}")
    return 0


def cmd_import(argv: Sequence[str], deps: PlanCliDeps) -> int:
    parser = argparse.ArgumentParser(prog="axi plan import")
    parser.add_argument("path")
    parser.add_argument("--scope", default=None)
    parser.add_argument("--goal", default=None)
    parser.add_argument("--principal", default=None)
    parser.add_argument("--accountable", default=None)
    args = parser.parse_args(list(argv))

    src = Path(args.path)
    if not src.exists():
        print(f"axi plan import: file not found: {src}", file=sys.stderr)
        return 2

    text = src.read_text(encoding="utf-8")
    front, body = _parse_frontmatter(text)
    if not body:
        body = text  # tolerant of files without frontmatter

    try:
        body_goal, steps = _parse_body(body)
    except PlanParseError as exc:
        print(f"axi plan import: parse failed: {exc}", file=sys.stderr)
        return exc.exit_code

    goal = args.goal or body_goal or "(no goal)"
    scope_id = args.scope or front.get("scope_id") or "default"
    principal = args.principal or deps.current_user
    accountable = args.accountable or principal
    request = PlanRequest(
        goal=goal,
        scope_id=scope_id,
        principal_id=principal,
        accountable_human_id=accountable,
    )
    new_plan = Plan(request=request, steps=steps)
    raci = RaciAssignment(
        responsible=(accountable,),
        accountable=(accountable,),
    )
    deps.store.save(new_plan, raci=raci)

    # Re-render the source file with frontmatter banner, so future reads
    # know it's a render artifact.
    src.write_text(_render_md(new_plan), encoding="utf-8")
    print(f"plan_id: {new_plan.plan_id}")
    print(f"imported_from: {src}")
    return 0


def cmd_refresh(argv: Sequence[str], deps: PlanCliDeps) -> int:
    parser = argparse.ArgumentParser(prog="axi plan refresh")
    parser.add_argument("plan_id")
    parser.add_argument("--md-out", default=None)
    args = parser.parse_args(list(argv))

    try:
        plan = deps.store.load(args.plan_id)
    except PlanNotFound as exc:
        print(f"axi plan: {exc}", file=sys.stderr)
        return exc.exit_code

    md_path = Path(args.md_out) if args.md_out else _default_md_path(plan.plan_id)
    _write_md(plan, md_path)
    print(f"refreshed: {md_path}")
    return 0


def _record_decision(
    *,
    deps: PlanCliDeps,
    plan_id: str,
    rationale: str,
    outcome: ApprovalOutcome,
    new_status: PlanStatus,
) -> int:
    try:
        plan = deps.store.load(plan_id)
    except PlanNotFound as exc:
        print(f"axi plan: {exc}", file=sys.stderr)
        return exc.exit_code

    raci = deps.store.raci_for(plan_id)
    if not raci.can_approve(deps.current_user):
        print(
            f"axi plan: principal {deps.current_user!r} not authorized "
            f"to act on plan {plan_id} (not in R or A)",
            file=sys.stderr,
        )
        return 4

    decision = ApprovalDecision(
        step_id=plan_id,  # plan-level decision; reuse step_id slot
        outcome=outcome,
        principal_id=deps.current_user,
        rationale=rationale,
    )
    deps.gate.record(decision)
    deps.store.save(plan.with_status(new_status), raci=raci)
    print(f"plan_id: {plan_id}")
    print(f"outcome: {outcome.value}")
    print(f"status: {new_status.value}")
    return 0


def cmd_approve(argv: Sequence[str], deps: PlanCliDeps) -> int:
    parser = argparse.ArgumentParser(prog="axi plan approve")
    parser.add_argument("plan_id")
    parser.add_argument("--rationale", required=True)
    args = parser.parse_args(list(argv))
    return _record_decision(
        deps=deps,
        plan_id=args.plan_id,
        rationale=args.rationale,
        outcome=ApprovalOutcome.APPROVED,
        new_status=PlanStatus.APPROVED,
    )


def cmd_reject(argv: Sequence[str], deps: PlanCliDeps) -> int:
    parser = argparse.ArgumentParser(prog="axi plan reject")
    parser.add_argument("plan_id")
    parser.add_argument("--rationale", required=True)
    args = parser.parse_args(list(argv))
    return _record_decision(
        deps=deps,
        plan_id=args.plan_id,
        rationale=args.rationale,
        outcome=ApprovalOutcome.REJECTED,
        new_status=PlanStatus.REJECTED,
    )


def cmd_list(argv: Sequence[str], deps: PlanCliDeps) -> int:
    parser = argparse.ArgumentParser(prog="axi plan list")
    parser.add_argument("--scope", default=None)
    parser.add_argument("--status", default=None)
    args = parser.parse_args(list(argv))

    status = _resolve_status(args.status) if args.status else None
    plans = list(deps.store.list_plans(scope_id=args.scope, status=status))
    if not plans:
        print("(no plans)")
        return 0
    for plan in plans:
        print(
            f"{plan.plan_id}  "
            f"status={plan.status.value:<10} "
            f"scope={plan.request.scope_id:<20} "
            f"goal={plan.request.goal[:60]}"
        )
    return 0


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


_HANDLERS: dict[str, Callable[[Sequence[str], PlanCliDeps], int]] = {
    "new": cmd_new,
    "show": cmd_show,
    "edit": cmd_edit,
    "import": cmd_import,
    "refresh": cmd_refresh,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "list": cmd_list,
}


def _build_default_deps() -> PlanCliDeps:
    """Construct production-ish deps. Persistence is in-memory until #76."""
    store = _InMemoryPlanStore()
    gate = ApprovalGate()

    def _derive_stub(request: PlanRequest) -> Plan:
        # Until AskBackedPlanPipeline is wired with a real Gateway here, we
        # emit a single placeholder step. This is a minimal default so the
        # CLI is testable end-to-end; the LLM-driven derive lands once
        # AskPipeline + Gateway construction is wired in main() (TBD with
        # the persistence task).
        step = PlanStep(
            intent=f"derive plan for: {request.goal}",
            tool_id=None,
        )
        return Plan(request=request, steps=(step,))

    user = os.environ.get("USER", "local")
    return PlanCliDeps(
        store=store,
        gate=gate,
        derive_fn=_derive_stub,
        current_user=f"@{user}:cohort.local",
    )


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        build_parser().print_help()
        return 0
    action, *rest = argv
    handler = _HANDLERS.get(action)
    if handler is None:
        print(f"axi plan: unknown action {action!r}", file=sys.stderr)
        print(f"available: {', '.join(_HANDLERS)}", file=sys.stderr)
        return 1
    deps = _build_default_deps()
    try:
        return handler(rest, deps)
    except PlanCliError as exc:
        print(f"axi plan: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
