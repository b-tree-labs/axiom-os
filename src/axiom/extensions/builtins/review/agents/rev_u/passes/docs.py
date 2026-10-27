# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Docs pass — missing docstrings, stale comments, API contract drift."""

from __future__ import annotations

import logging

from axiom.extensions.builtins.review.agents.rev_u.passes._parse import (
    degraded,
    parse_findings,
)
from axiom.extensions.builtins.review.tools.findings import Finding

log = logging.getLogger(__name__)

PASS_KIND = "docs"

SYSTEM_PROMPT = """\
You are a documentation reviewer for Python code changes.
Focus ONLY on: public functions or classes that lack docstrings, comments that
describe the wrong behavior (stale/incorrect), parameter or return types that
are undocumented in public APIs, docstrings that contradict the implementation,
and API contract drift where the implementation changed but documentation did not.

Do NOT flag correctness, performance, security, or test coverage issues.

Output a JSON array of findings. Each finding has these fields:
  severity: "blocker" | "major" | "minor" | "nit"
  path: relative file path
  line: line number (integer) or null
  message: concise description of the documentation issue
  suggested_fix: brief fix suggestion or null

Respond with ONLY the JSON array, no prose. If there are no findings, return [].
"""


def run(diff: str, ctx: dict, llm) -> list[Finding]:
    """Run the docs pass and return a list of Findings."""
    prompt = _build_prompt(diff, ctx)
    try:
        response = llm.complete(prompt, system=SYSTEM_PROMPT)
        return parse_findings(response.text, pass_kind=PASS_KIND)
    except Exception as exc:
        # Returning [] here reported a clean pass for a network blip, an
        # expired key, or an answer we could not read. Say what happened
        # instead, in the result the reviewer actually reads.
        log.warning("docs pass failed: %s", exc)
        return [degraded(PASS_KIND, str(exc))]


def _build_prompt(diff: str, ctx: dict) -> str:
    parts = ["Review the following diff for documentation issues.\n\n## Diff\n\n```diff"]
    parts.append(diff)
    parts.append("```")
    if ctx:
        parts.append("\n## Full file context\n")
        for path, content in list(ctx.items())[:10]:
            parts.append(f"\n### {path}\n```python\n{content}\n```")
    return "\n".join(parts)


__all__ = ["run", "SYSTEM_PROMPT", "PASS_KIND"]
