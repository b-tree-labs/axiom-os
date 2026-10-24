# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Conformance: a start-up "resume pending work" hook must take a claim.

ADR-182 D3a. During a zero-gap switch an old and a new copy run together; a
function named ``resume_pending*`` that finishes spooled items without
:mod:`axiom.infra.pending_claim` will finish an item the old copy is still
working on. The check is by name and by import, which is crude on purpose: it
turns the next such hook into a failing test instead of a review comment.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "axiom"
HOOK = re.compile(r"^_?resume_pending\w*$")


def _hooks():
    for path in SRC.rglob("*.py"):
        if "/tests/" in str(path) or path.name == "pending_claim.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "resume_pending" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and HOOK.match(node.name):
                yield path, node, text


def test_every_resume_pending_hook_uses_the_claim_primitive():
    found = list(_hooks())
    assert found, "the scan found no resume_pending hook at all: the pattern has drifted"
    bare = [f"{p.relative_to(SRC)}::{n.name}" for p, n, text in found if "pending_claim" not in ast.get_source_segment(text, n)]
    assert not bare, (
        "a resume_pending hook must claim each item via axiom.infra.pending_claim "
        f"(ADR-182 D3a), or two overlapping copies answer one item twice: {bare}"
    )
