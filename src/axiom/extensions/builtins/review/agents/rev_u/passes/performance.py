# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Performance pass — algorithmic complexity, N+1 queries, unnecessary allocations."""

from __future__ import annotations

import json
import logging

from axiom.extensions.builtins.review.tools.findings import Finding

log = logging.getLogger(__name__)

PASS_KIND = "performance"

SYSTEM_PROMPT = """\
You are a performance reviewer for Python code changes.
Focus ONLY on: O(n²) or worse algorithms where O(n) or O(log n) is feasible,
N+1 database/API query patterns, repeated computation that should be cached,
unnecessary list copies or large intermediate collections, blocking I/O in
hot paths, and memory leaks from unbounded data structures.

Do NOT flag correctness, style, documentation, security, or test coverage issues.

Output a JSON array of findings. Each finding has these fields:
  severity: "blocker" | "major" | "minor" | "nit"
  path: relative file path
  line: line number (integer) or null
  message: concise description of the performance issue
  suggested_fix: brief fix suggestion or null

Respond with ONLY the JSON array, no prose. If there are no findings, return [].
"""


def run(diff: str, ctx: dict, llm) -> list[Finding]:
    """Run the performance pass and return a list of Findings."""
    prompt = _build_prompt(diff, ctx)
    try:
        response = llm.complete(prompt, system=SYSTEM_PROMPT)
        return _parse_response(response.text)
    except Exception as exc:
        log.warning("performance pass failed: %s", exc)
        return []


def _build_prompt(diff: str, ctx: dict) -> str:
    parts = ["Review the following diff for performance issues.\n\n## Diff\n\n```diff"]
    parts.append(diff)
    parts.append("```")
    if ctx:
        parts.append("\n## Full file context\n")
        for path, content in list(ctx.items())[:10]:
            parts.append(f"\n### {path}\n```python\n{content}\n```")
    return "\n".join(parts)


def _parse_response(text: str) -> list[Finding]:
    try:
        raw = json.loads(text.strip())
        if not isinstance(raw, list):
            return []
        findings = []
        for item in raw:
            findings.append(
                Finding(
                    severity=item.get("severity", "minor"),
                    pass_kind=PASS_KIND,
                    path=item.get("path", ""),
                    line=item.get("line"),
                    message=item.get("message", ""),
                    suggested_fix=item.get("suggested_fix"),
                )
            )
        return findings
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


__all__ = ["run", "SYSTEM_PROMPT", "PASS_KIND"]
