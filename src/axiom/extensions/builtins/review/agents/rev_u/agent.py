# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""REV-U — multi-pass diff review agent.

Runs 5 specialized review passes (correctness, performance, security, docs,
tests) against a unified diff, aggregates findings, and optionally validates
them to drop hallucinations.

Phase 2+ features (GitHub ingestion, memory integration, plan-first mode,
Linus-mode retraction, sandbox test execution) are deferred — see
docs/prds/prd-rev-u-pr-review.md.
"""

from __future__ import annotations

import logging
from typing import Any

from axiom.extensions.builtins.review.agents.rev_u.passes import (
    correctness,
    docs,
    performance,
    security,
)
from axiom.extensions.builtins.review.agents.rev_u.passes import tests as tests_pass
from axiom.extensions.builtins.review.agents.rev_u.validator import validate
from axiom.extensions.builtins.review.tools.context import gather_context
from axiom.extensions.builtins.review.tools.findings import Finding, FindingSet

log = logging.getLogger(__name__)

_ALL_PASSES: dict[str, Any] = {
    "correctness": correctness,
    "performance": performance,
    "security": security,
    "docs": docs,
    "tests": tests_pass,
}


def _get_llm(llm: Any | None) -> Any:
    """Resolve the LLM to use.

    Accepts an injected llm for testing. In production resolves via Gateway.
    # TODO(Phase 2+): route through axiom.policy.llm_tier when that module exists.
    """
    if llm is not None:
        return llm
    try:
        from axiom.infra.gateway import Gateway

        return Gateway()
    except Exception as exc:
        raise RuntimeError(
            "No LLM available — pass an llm= argument or configure a provider "
            "via `axi connect`."
        ) from exc


class RevUAgent:
    """Multi-pass diff review agent.

    Args:
        repo_root: Absolute or relative path to the repository root.
        llm: Optional LLM instance (for testing). If None, resolved via Gateway.
    """

    def __init__(self, repo_root: str = ".", llm: Any | None = None) -> None:
        self.repo_root = repo_root
        self._llm = llm

    def review(
        self,
        diff: str,
        *,
        passes: list[str] | None = None,
        run_validator: bool = True,
    ) -> FindingSet:
        """Run review passes against *diff* and return aggregated findings.

        Args:
            diff: Unified diff string.
            passes: List of pass kind names to run. Defaults to all 5.
            run_validator: Whether to run the validator gate. Default True.

        Returns:
            FindingSet containing all (validated) findings.
        """
        selected = passes if passes else list(_ALL_PASSES.keys())
        llm = _get_llm(self._llm)

        ctx, ctx_warnings = gather_context(diff, repo_root=self.repo_root)

        all_findings: list[Finding] = list(ctx_warnings)

        for pass_name in selected:
            pass_module = _ALL_PASSES.get(pass_name)
            if pass_module is None:
                log.warning("unknown pass %r — skipping", pass_name)
                continue
            try:
                results = pass_module.run(diff, ctx, llm)
                all_findings.extend(results)
            except Exception as exc:
                log.warning("pass %r raised an exception: %s", pass_name, exc)
                # Continue with remaining passes per acceptance criteria.

        if run_validator:
            all_findings = validate(all_findings, diff)

        return FindingSet(findings=all_findings)


__all__ = ["RevUAgent"]
