# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fleet console — effect-checked status over nodes that push reports out.

ADR-119: push-only ingestion (the console never reaches into a node),
GREEN requires cited observed-effect evidence, silence is failure.
PRD: docs/prds/prd-fleet-console.md. Spec: docs/specs/spec-fleet-console.md.
"""

from axiom.extensions.builtins.fleet.status import (
    Evaluation,
    Status,
    evaluate_report,
    rollup,
)

__all__ = ["Evaluation", "Status", "evaluate_report", "rollup"]
