# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axi plan`` CLI subcommand tree.

Per analysis §10.1, five user-facing flows + two gate-workflow commands +
``list``. Memory is canonical; ``.md`` is a render artifact OR an
explicit-import draft. Tests stub the persistence layer (PlanStore) and the
derive pipeline so we can drive every flow without an LLM round-trip.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from axiom.agents.pipeline.gates import (
    ApprovalGate,
    ApprovalOutcome,
    RaciAssignment,
)
from axiom.agents.pipeline.plan import (
    Plan,
    PlanRequest,
    PlanStatus,
    PlanStep,
    PlanStepGate,
    StepReach,
)
from axiom.cli import plan as plan_cli

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubPlanStore:
    """In-memory PlanStore implementation for tests.

    Mirrors the Protocol declared in axiom.cli.plan. Records writes + reads
    so tests can assert on round-trips. The persistence task (#76) lands the
    real implementation; integration is a 1-line swap.
    """

    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}
        self._raci: dict[str, RaciAssignment] = {}
        self.write_log: list[str] = []

    def save(self, plan: Plan, *, raci: RaciAssignment | None = None) -> None:
        self._plans[plan.plan_id] = plan
        if raci is not None:
            self._raci[plan.plan_id] = raci
        self.write_log.append(plan.plan_id)

    def load(self, plan_id: str) -> Plan:
        try:
            return self._plans[plan_id]
        except KeyError as exc:
            raise plan_cli.PlanNotFound(plan_id) from exc

    def raci_for(self, plan_id: str) -> RaciAssignment:
        if plan_id in self._raci:
            return self._raci[plan_id]
        plan = self.load(plan_id)
        # Default per task: accountable_human_id is both R and A.
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
        out = []
        for plan in self._plans.values():
            if scope_id and plan.request.scope_id != scope_id:
                continue
            if status and plan.status != status:
                continue
            out.append(plan)
        return out


def _stub_pipeline_factory(steps_for_goal: dict[str, tuple[PlanStep, ...]] | None = None):
    """Return a derive callable that produces fixed steps for a request."""
    default_steps = (
        PlanStep(
            intent="gather context",
            tool_id="rag.search",
            reach=StepReach(reads=("docs/**",)),
        ),
        PlanStep(
            intent="draft response",
            tool_id="compose.write",
            reach=StepReach(writes=("draft.md",)),
            gate=PlanStepGate.APPROVE,
        ),
    )

    def _derive(request: PlanRequest) -> Plan:
        steps = (steps_for_goal or {}).get(request.goal, default_steps)
        return Plan(request=request, steps=steps)

    return _derive


@pytest.fixture
def store() -> _StubPlanStore:
    return _StubPlanStore()


@pytest.fixture
def gate() -> ApprovalGate:
    return ApprovalGate()


@pytest.fixture
def deps(store, gate, tmp_path, monkeypatch):
    """Wire up CLI deps, force cwd to tmp_path so plans/ lands there."""
    monkeypatch.chdir(tmp_path)
    return plan_cli.PlanCliDeps(
        store=store,
        gate=gate,
        derive_fn=_stub_pipeline_factory(),
        editor_runner=lambda path: None,
        current_user="@alice:cohort.test",
    )


# ---------------------------------------------------------------------------
# axi plan new
# ---------------------------------------------------------------------------


def test_new_derives_via_pipeline_and_writes_to_store(deps, store, capsys, tmp_path):
    rc = plan_cli.cmd_new(
        argv=[
            "--goal",
            "ship hello world",
            "--scope",
            "scope-A",
        ],
        deps=deps,
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "plan_id" in captured.out.lower() or len(store.write_log) == 1
    assert len(store.write_log) == 1
    plan_id = store.write_log[0]
    plan = store.load(plan_id)
    assert plan.request.goal == "ship hello world"
    assert plan.request.scope_id == "scope-A"
    assert plan.request.principal_id == "@alice:cohort.test"
    assert plan.request.accountable_human_id == "@alice:cohort.test"
    # .md artifact written
    md_path = tmp_path / "plans" / f"{plan_id}.md"
    assert md_path.exists(), f"expected rendered plan at {md_path}"


def test_new_creates_plans_dir_if_missing(deps, store, tmp_path):
    plans_dir = tmp_path / "plans"
    assert not plans_dir.exists()
    rc = plan_cli.cmd_new(
        argv=["--goal", "x", "--scope", "s"],
        deps=deps,
    )
    assert rc == 0
    assert plans_dir.is_dir()


def test_new_accepts_explicit_principal_and_accountable(deps, store):
    rc = plan_cli.cmd_new(
        argv=[
            "--goal",
            "g",
            "--scope",
            "s",
            "--principal",
            "@bot:cohort.test",
            "--accountable",
            "@bob:cohort.test",
        ],
        deps=deps,
    )
    assert rc == 0
    plan = store.load(store.write_log[0])
    assert plan.request.principal_id == "@bot:cohort.test"
    assert plan.request.accountable_human_id == "@bob:cohort.test"


# ---------------------------------------------------------------------------
# axi plan show
# ---------------------------------------------------------------------------


def test_show_renders_to_console_and_writes_md(deps, store, capsys, tmp_path):
    plan_cli.cmd_new(argv=["--goal", "g", "--scope", "s"], deps=deps)
    plan_id = store.write_log[0]
    capsys.readouterr()  # discard new output

    rc = plan_cli.cmd_show(argv=[plan_id], deps=deps)
    assert rc == 0
    out = capsys.readouterr().out
    # console body has goal + steps
    assert "g" in out  # goal
    assert "gather context" in out
    assert "draft response" in out
    # .md written at default plans/<id>.md
    md_path = tmp_path / "plans" / f"{plan_id}.md"
    assert md_path.exists()


def test_show_md_carries_frontmatter_banner(deps, store, tmp_path):
    plan_cli.cmd_new(argv=["--goal", "frontmatter check", "--scope", "s"], deps=deps)
    plan_id = store.write_log[0]

    plan_cli.cmd_show(argv=[plan_id], deps=deps)
    md_path = tmp_path / "plans" / f"{plan_id}.md"
    text = md_path.read_text()
    assert text.startswith("---\n")
    assert f"plan_id: {plan_id}" in text
    assert f"rendered_from: {plan_id}" in text
    assert "rendered_at:" in text
    assert "status:" in text
    assert "Memory is canonical" in text
    assert "axi plan import" in text or "re-imported" in text


def test_show_md_body_lists_steps(deps, store, tmp_path):
    plan_cli.cmd_new(argv=["--goal", "list-steps test", "--scope", "s"], deps=deps)
    plan_id = store.write_log[0]
    plan = store.load(plan_id)

    plan_cli.cmd_show(argv=[plan_id], deps=deps)
    md_path = tmp_path / "plans" / f"{plan_id}.md"
    body = md_path.read_text()
    assert "# Plan: list-steps test" in body
    assert "## Steps" in body
    for step in plan.steps:
        assert step.intent in body
        if step.tool_id:
            assert step.tool_id in body
    # reach summary text from summarize_reach()
    assert "reads 1 path" in body
    assert "writes 1 path" in body


def test_show_writes_md_to_explicit_path(deps, store, tmp_path):
    plan_cli.cmd_new(argv=["--goal", "g", "--scope", "s"], deps=deps)
    plan_id = store.write_log[0]
    out_path = tmp_path / "custom" / "plan.md"
    rc = plan_cli.cmd_show(argv=[plan_id, "--md-out", str(out_path)], deps=deps)
    assert rc == 0
    assert out_path.exists()
    assert f"plan_id: {plan_id}" in out_path.read_text()


def test_show_unknown_plan_returns_error(deps, capsys):
    rc = plan_cli.cmd_show(argv=["does-not-exist"], deps=deps)
    assert rc != 0
    err = capsys.readouterr().err
    assert "does-not-exist" in err or "not found" in err.lower()


# ---------------------------------------------------------------------------
# axi plan edit
# ---------------------------------------------------------------------------


def test_edit_invokes_editor_and_supersedes(deps, store, tmp_path, monkeypatch):
    plan_cli.cmd_new(argv=["--goal", "edit-me", "--scope", "s"], deps=deps)
    original_id = store.write_log[0]

    def _editor(path: Path) -> None:
        # Simulate the user replacing the body of the .md.
        text = path.read_text()
        # Strip frontmatter and append a single replacement step list.
        text.split("---\n", 2)[-1]
        new_body = (
            "# Plan: edit-me (revised)\n\n"
            "## Steps\n\n"
            "- intent: refined step one\n"
            "  tool_id: rag.search\n"
            "- intent: refined step two\n"
            "  tool_id: compose.write\n"
        )
        # Preserve original frontmatter so the parser sees it; just rewrite body
        head, _, _ = text.partition("\n---\n")
        path.write_text(head + "\n---\n" + new_body)

    deps2 = plan_cli.PlanCliDeps(
        store=deps.store,
        gate=deps.gate,
        derive_fn=deps.derive_fn,
        editor_runner=_editor,
        current_user=deps.current_user,
    )

    rc = plan_cli.cmd_edit(argv=[original_id], deps=deps2)
    assert rc == 0
    # New plan version saved with supersedes set to the original.
    assert len(store.write_log) == 2
    new_id = store.write_log[1]
    assert new_id != original_id
    new_plan = store.load(new_id)
    assert new_plan.supersedes == original_id
    intents = [s.intent for s in new_plan.steps]
    assert "refined step one" in intents
    assert "refined step two" in intents


# ---------------------------------------------------------------------------
# axi plan import
# ---------------------------------------------------------------------------


def test_import_parses_md_and_writes_new_plan(deps, store, tmp_path, capsys):
    src = tmp_path / "external.md"
    src.write_text(
        "# Plan: external goal\n\n"
        "## Steps\n\n"
        "- intent: imported step a\n"
        "  tool_id: rag.search\n"
        "- intent: imported step b\n"
    )
    rc = plan_cli.cmd_import(
        argv=[str(src), "--scope", "scope-import"],
        deps=deps,
    )
    assert rc == 0
    assert len(store.write_log) == 1
    plan_id = store.write_log[0]
    plan = store.load(plan_id)
    assert plan.request.scope_id == "scope-import"
    intents = [s.intent for s in plan.steps]
    assert intents == ["imported step a", "imported step b"]
    # File re-rendered with frontmatter banner.
    rendered = src.read_text()
    assert rendered.startswith("---\n")
    assert f"plan_id: {plan_id}" in rendered


# ---------------------------------------------------------------------------
# axi plan refresh
# ---------------------------------------------------------------------------


def test_refresh_re_renders_without_re_deriving(deps, store, tmp_path):
    plan_cli.cmd_new(argv=["--goal", "refresh-me", "--scope", "s"], deps=deps)
    plan_id = store.write_log[0]
    md_path = tmp_path / "plans" / f"{plan_id}.md"
    md_path.unlink()
    assert not md_path.exists()

    rc = plan_cli.cmd_refresh(argv=[plan_id], deps=deps)
    assert rc == 0
    assert md_path.exists()
    # Same plan; no second save.
    assert len(store.write_log) == 1


# ---------------------------------------------------------------------------
# axi plan approve
# ---------------------------------------------------------------------------


def test_approve_records_and_transitions_to_approved(deps, store, gate):
    plan_cli.cmd_new(argv=["--goal", "approve-me", "--scope", "s"], deps=deps)
    plan_id = store.write_log[0]
    rc = plan_cli.cmd_approve(
        argv=[plan_id, "--rationale", "looks good"],
        deps=deps,
    )
    assert rc == 0
    plan = store.load(plan_id)
    assert plan.status == PlanStatus.APPROVED
    decisions = gate.decisions_for_step(plan_id)
    assert len(decisions) == 1
    assert decisions[0].outcome == ApprovalOutcome.APPROVED
    assert decisions[0].rationale == "looks good"


def test_approve_rejects_unauthorized_principal(deps, store, gate, capsys):
    plan_cli.cmd_new(argv=["--goal", "g", "--scope", "s"], deps=deps)
    plan_id = store.write_log[0]
    bad_deps = plan_cli.PlanCliDeps(
        store=deps.store,
        gate=deps.gate,
        derive_fn=deps.derive_fn,
        editor_runner=deps.editor_runner,
        current_user="@mallory:cohort.evil",
    )
    rc = plan_cli.cmd_approve(
        argv=[plan_id, "--rationale", "sneak it in"],
        deps=bad_deps,
    )
    assert rc != 0
    plan = store.load(plan_id)
    assert plan.status == PlanStatus.DRAFT  # unchanged
    err = capsys.readouterr().err
    assert "not authorized" in err.lower() or "unauthorized" in err.lower()


# ---------------------------------------------------------------------------
# axi plan reject
# ---------------------------------------------------------------------------


def test_reject_records_and_transitions_to_rejected(deps, store, gate):
    plan_cli.cmd_new(argv=["--goal", "reject-me", "--scope", "s"], deps=deps)
    plan_id = store.write_log[0]
    rc = plan_cli.cmd_reject(
        argv=[plan_id, "--rationale", "scope creep"],
        deps=deps,
    )
    assert rc == 0
    plan = store.load(plan_id)
    assert plan.status == PlanStatus.REJECTED
    decisions = gate.decisions_for_step(plan_id)
    assert decisions[-1].outcome == ApprovalOutcome.REJECTED
    assert decisions[-1].rationale == "scope creep"


# ---------------------------------------------------------------------------
# axi plan list
# ---------------------------------------------------------------------------


def test_list_returns_plans_from_store(deps, store, capsys):
    plan_cli.cmd_new(argv=["--goal", "g1", "--scope", "scope-X"], deps=deps)
    plan_cli.cmd_new(argv=["--goal", "g2", "--scope", "scope-X"], deps=deps)
    plan_cli.cmd_new(argv=["--goal", "g3", "--scope", "scope-Y"], deps=deps)
    capsys.readouterr()  # discard

    rc = plan_cli.cmd_list(argv=["--scope", "scope-X"], deps=deps)
    assert rc == 0
    out = capsys.readouterr().out
    # Both scope-X plan ids should appear in output.
    p_x_ids = [pid for pid in store.write_log if store.load(pid).request.scope_id == "scope-X"]
    assert len(p_x_ids) == 2
    for pid in p_x_ids:
        assert pid in out
    # The scope-Y plan should not appear.
    p_y_ids = [pid for pid in store.write_log if store.load(pid).request.scope_id == "scope-Y"]
    for pid in p_y_ids:
        assert pid not in out


def test_list_filters_by_status(deps, store, gate, capsys):
    plan_cli.cmd_new(argv=["--goal", "a", "--scope", "s"], deps=deps)
    plan_cli.cmd_new(argv=["--goal", "b", "--scope", "s"], deps=deps)
    approved_id = store.write_log[0]
    plan_cli.cmd_approve(argv=[approved_id, "--rationale", "ok"], deps=deps)
    capsys.readouterr()

    rc = plan_cli.cmd_list(argv=["--status", "approved"], deps=deps)
    assert rc == 0
    out = capsys.readouterr().out
    assert approved_id in out
    other_id = store.write_log[1]
    assert other_id not in out


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------


def test_parse_frontmatter_extracts_block_and_body():
    text = (
        "---\n"
        "plan_id: abc\n"
        "status: draft\n"
        "---\n"
        "body content here\n"
    )
    front, body = plan_cli._parse_frontmatter(text)
    assert front["plan_id"] == "abc"
    assert front["status"] == "draft"
    assert "body content here" in body


def test_parse_frontmatter_missing_returns_empty_dict():
    text = "no frontmatter here\n## Steps\n- intent: x\n"
    front, body = plan_cli._parse_frontmatter(text)
    assert front == {}
    assert body == text


# ---------------------------------------------------------------------------
# Body parser (markdown -> steps)
# ---------------------------------------------------------------------------


def test_parse_body_extracts_steps():
    body = (
        "# Plan: anything\n\n"
        "## Steps\n\n"
        "- intent: first step\n"
        "  tool_id: rag.search\n"
        "- intent: second step\n"
        "  tool_id: compose.write\n"
    )
    goal, steps = plan_cli._parse_body(body)
    assert goal == "anything"
    intents = [s.intent for s in steps]
    assert intents == ["first step", "second step"]
    assert steps[0].tool_id == "rag.search"
    assert steps[1].tool_id == "compose.write"


def test_parse_body_handles_no_tool_id():
    body = (
        "# Plan: g\n\n"
        "## Steps\n\n"
        "- intent: only intent\n"
    )
    _, steps = plan_cli._parse_body(body)
    assert len(steps) == 1
    assert steps[0].intent == "only intent"
    assert steps[0].tool_id is None


# ---------------------------------------------------------------------------
# build_parser smoke
# ---------------------------------------------------------------------------


def test_build_parser_exposes_all_subcommands():
    parser = plan_cli.build_parser()
    # Force argparse to enumerate.
    sub_actions = [
        a for a in parser._actions
        if a.__class__.__name__ == "_SubParsersAction"
    ]
    assert sub_actions, "expected subparsers"
    choices = set(sub_actions[0].choices.keys())
    expected = {"new", "show", "edit", "import", "refresh", "approve", "reject", "list"}
    assert expected.issubset(choices), f"missing: {expected - choices}"
