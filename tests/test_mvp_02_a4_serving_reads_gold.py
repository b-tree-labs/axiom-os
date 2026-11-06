# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MVP-02-A4 + MVP-11-A1: chat never reads silver, and no consumer path does.

``tests/test_medallion_tier_boundaries.py`` refuses a SQL read of a working tier
anywhere in ``src/axiom`` outside the writers and the listed diagnostics. Two
ways round it were still open, and this file closes them for the serving face:

1. A working-tier object named as an IDENTIFIER rather than in a query
   (``source="silver.signals"``) handed to a reader that builds the SQL.
2. A consumer reaching a working tier THROUGH a capability: a chat or a served
   data-door request that says ``tier=silver``, names ``silver.signals``, or
   calls a diagnostic that reads silver by design. Those are refused with a
   reason that points at gold (DC-12).
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from axiom.extensions.builtins.data_platform.tiers import (
    WORKING_TIER_CAPABILITIES,
    WORKING_TIERS,
    serving_refusal,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
_B = ROOT / "src" / "axiom" / "extensions" / "builtins"
_DP = _B / "data_platform"

#: The serving face: everything whose output reaches a person, an agent, a page, a
#: chart, an MCP client or an export. Whole packages, so a new module is covered.
CONSUMER_PACKAGES = (
    "chat",
    "mcp",
    "webapp",
    "charts",
    "scidisplay",
    "receipts",
    "lane",
    "search",
    "library",
    "sites",
    "signals",
)

#: The data platform's own serving modules: the gold door and the friendly verbs over it.
CONSUMER_MODULES = (
    "read_api.py",
    "read_mount.py",
    "read_target.py",
    "friendly.py",
    "friendly_text.py",
    "skills/gold.py",
    "skills/friendly.py",
    "ingest_sink/summary.py",
    "skills/ingest_freshness.py",
)

#: A SQL read of a working tier (the same shape the tier-boundary guard refuses).
_QUERY = re.compile(
    r"\b(from|join)\s+\"?(" + "|".join(WORKING_TIERS) + r")\"?\.\"?\w", re.IGNORECASE
)
#: A working-tier object named as an identifier in a string constant.
_IDENT = re.compile(r"\b(" + "|".join(WORKING_TIERS) + r")\.[a-z_]\w*")

#: Consumer files allowed to name a working-tier identifier in a string, each with why.
#: Empty, and it should stay that way: a serving path names gold.
ALLOWED: dict[str, str] = {}


def _consumer_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for pkg in CONSUMER_PACKAGES:
        files.extend(p for p in (_B / pkg).rglob("*.py") if "/tests/" not in p.as_posix())
    files.extend(_DP / m for m in CONSUMER_MODULES if (_DP / m).exists())
    return files


def _code_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    """String constants that are code, not documentation (docstrings left out)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prose: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            prose.add(id(node.value))
    return [
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in prose
    ]


def test_mvp_11_a1_the_scan_covers_the_serving_face() -> None:
    files = _consumer_files()
    rel = {f.relative_to(_B).as_posix() for f in files}
    for must in (
        "chat/agent.py",
        "mcp/skill_tools.py",
        "webapp/api/catalog.py",
        "webapp/catalog/series.py",
        "charts/api.py",
        "data_platform/read_api.py",
        "data_platform/skills/gold.py",
    ):
        assert must in rel, f"{must} is a serving path the scan must read"


def test_mvp_11_a1_no_consumer_path_reads_silver() -> None:
    offenders: list[str] = []
    for path in _consumer_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        for line, text in _code_strings(path):
            if _QUERY.search(text) or _IDENT.search(text):
                offenders.append(f"{rel}:{line}: {text[:80]!r}")
    assert not offenders, (
        "a serving path names a working tier; serve from gold (gold.signals, a gold verb or "
        "a kit's gold object), or add the gold object it needs: " + "; ".join(offenders)
    )


def test_mvp_11_a1_the_allowlist_has_no_stale_entries() -> None:
    stale = [n for n in ALLOWED if not (ROOT / n).exists()]
    assert not stale, f"allowlisted files that are gone: {stale}"


def test_mvp_11_a1_the_served_gold_verbs_never_pass_a_medallion_tier() -> None:
    """``gold_query`` can answer from silver when handed ``tier="silver"`` (for the
    engineering verbs); the served gold skills must never hand it one."""
    tree = ast.parse((_DP / "skills" / "gold.py").read_text(encoding="utf-8"))
    passed = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.keyword) and n.arg == "tier"]
    assert not passed, f"skills/gold.py passes tier= to the executor at lines {passed}"


# --- the refusal, with a reason ----------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"tier": "silver"},
        {"tier": "Silver", "object": "signals"},
        {"table": "silver.signals", "column": "value", "fn": "max"},
        {"object": "bronze.batches"},
        {"source": '"silver"."signals"'},
    ],
)
def test_mvp_02_a4_a_request_naming_silver_is_refused_with_a_reason(args: dict) -> None:
    why = serving_refusal("data.describe", args)
    assert why is not None and why.startswith("refused:")
    assert "gold" in why


def test_mvp_02_a4_a_silver_diagnostic_is_refused_on_a_serving_surface() -> None:
    assert "data.gaps" in WORKING_TIER_CAPABILITIES
    for name in ("data.gaps", "data__gaps"):
        why = serving_refusal(name, {"site": "site-a"})
        assert why is not None and "gold" in why and "CLI or MCP" in why


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"tier": "gold"},
        {"table": "signals"},
        {"table": "gold.signals"},
        {"source": "silver.csv.d/notes"},
        {"site": "silver"},
    ],
)
def test_mvp_02_a4_a_gold_request_is_not_refused(args: dict) -> None:
    assert serving_refusal("data.series", args) is None


def test_mvp_02_a4_chat_refuses_a_silver_tool_call_before_it_runs() -> None:
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.scope import ChatScope
    from axiom.extensions.builtins.chat.tools import ToolDef
    from axiom.infra.gateway import CompletionResponse, ToolUseBlock
    from axiom.infra.orchestrator.actions import ActionCategory

    agent = ChatAgent.__new__(ChatAgent)
    agent._render = None  # type: ignore[attr-defined]
    scope = ChatScope()
    tools = {
        "data__describe": ToolDef(
            name="data__describe", description="d", category=ActionCategory.READ, parameters={}
        )
    }
    planned = CompletionResponse(
        text="",
        tool_use=[
            ToolUseBlock(
                tool_id="call_1",
                name="data__describe",
                input={"tier": "silver", "object": "signals"},
            ),
        ],
    )
    [(tool_id, name, result)] = agent._process_tool_calls(planned, tools, scope=scope)
    assert (tool_id, name) == ("call_1", "data__describe")
    assert result["error"].startswith("refused:") and "gold" in result["error"]
    assert scope.last_turn_tools == [], "a refused call never ran"


def test_mvp_02_a4_the_served_data_door_refuses_silver_with_a_reason(monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.data_platform import read_api

    monkeypatch.setattr(read_api, "holds_medallion", lambda: True)
    app = FastAPI()
    app.include_router(read_api.build_data_read_router())
    body = {
        "verb": "data.series",
        "params": {
            "table": "silver.signals",
            "column": "value",
            "bucket": "1 hour",
            "time_column": "ts",
        },
    }
    got = TestClient(app).post("/data/query", json=body).json()
    assert got["ok"] is False and got["errors"][0].startswith("refused:")
    assert "gold" in got["errors"][0]
