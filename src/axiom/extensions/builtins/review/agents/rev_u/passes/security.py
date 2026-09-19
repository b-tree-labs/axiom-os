# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Security pass — injection, secrets, auth bypass, input validation, OWASP Top 10."""

from __future__ import annotations

import logging

from axiom.extensions.builtins.review.agents.rev_u.passes._parse import (
    degraded,
    parse_findings,
)
from axiom.extensions.builtins.review.tools.findings import Finding

log = logging.getLogger(__name__)

PASS_KIND = "security"

SYSTEM_PROMPT = """\
You are a security reviewer for Python code changes.
Focus ONLY on: SQL injection, command injection, path traversal, XSS,
hardcoded secrets or credentials, authentication bypass, improper input
validation, insecure deserialization, SSRF, CSRF, broken access control,
and other OWASP Top 10 vulnerabilities.

Do NOT flag correctness, performance, style, documentation, or test coverage issues.

Output a JSON array of findings. Each finding has these fields:
  severity: "blocker" | "major" | "minor" | "nit"
  path: relative file path
  line: line number (integer) or null
  message: concise description of the security issue
  suggested_fix: brief fix suggestion or null

Respond with ONLY the JSON array, no prose. If there are no findings, return [].
"""


def run(diff: str, ctx: dict, llm) -> list[Finding]:
    """Run the security pass and return a list of Findings."""
    prompt = _build_prompt(diff, ctx)
    try:
        response = llm.complete(prompt, system=SYSTEM_PROMPT)
        return parse_findings(response.text, pass_kind=PASS_KIND)
    except Exception as exc:
        # Returning [] here reported a clean pass for a network blip, an
        # expired key, or an answer we could not read. Say what happened
        # instead, in the result the reviewer actually reads.
        log.warning("security pass failed: %s", exc)
        return [degraded(PASS_KIND, str(exc))]


def _build_prompt(diff: str, ctx: dict) -> str:
    parts = ["Review the following diff for security vulnerabilities.\n\n## Diff\n\n```diff"]
    parts.append(diff)
    parts.append("```")
    if ctx:
        parts.append("\n## Full file context\n")
        for path, content in list(ctx.items())[:10]:
            parts.append(f"\n### {path}\n```python\n{content}\n```")
    return "\n".join(parts)


__all__ = ["run", "SYSTEM_PROMPT", "PASS_KIND"]
