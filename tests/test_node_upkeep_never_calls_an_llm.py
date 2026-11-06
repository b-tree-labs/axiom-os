# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The services a confined node runs to look after itself never reach for a language model.

A collector-role node runs deterministic upkeep: landing, the outbox an edge or
forwarder ships from, conformance, the downstream pull, and the updater. A
site that declared no agents is told nothing on the machine calls a model; the
gateway enforces that at runtime, and this keeps the upkeep code from ever
needing the exception.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "axiom" / "extensions" / "builtins"
UPKEEP = [
    ROOT / "update",
    ROOT / "data_platform" / "ingest_sink",
    ROOT / "data_platform" / "conformance",
    ROOT / "data_platform" / "sources" / "edge",
    ROOT / "features",
]
FORBIDDEN = ("axiom.llm", "openai", "anthropic")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_upkeep_code_imports_no_language_model_client():
    offenders = []
    for base in UPKEEP:
        assert base.is_dir(), base
        for py in base.rglob("*.py"):
            if "tests" in py.parts:
                continue
            for mod in _imports(py):
                if mod.startswith(FORBIDDEN):
                    offenders.append(f"{py.relative_to(ROOT)} imports {mod}")
    assert offenders == []


def test_the_guard_can_fail(tmp_path):
    bad = tmp_path / "x.py"
    bad.write_text("from axiom.llm.gateway import Gateway\n")
    assert any(m.startswith(FORBIDDEN) for m in _imports(bad))
