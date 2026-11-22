# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ungoverned harness: the action body and nothing around it.

Not a placeholder. It is the floor every other harness is measured against, and
running it alongside a real one is how you check the engine is measuring
governance rather than itself — its `governed` variant is the bare body under a
different name, so any overhead it reports is the benchmark's own noise.
"""

from __future__ import annotations

from axiom.evals.overhead import COMPOSED_VARIANT, Harness, Variant, action_body


def build() -> Harness:
    return Harness(
        name="none",
        notes="no governance: the shared action body, unwrapped",
        variants=[
            Variant(name="bare", run=action_body, guarantees=(),
                    description="the shared action body"),
            Variant(name=COMPOSED_VARIANT, run=action_body, guarantees=(),
                    description="identical to bare — the engine's own noise floor, "
                                "and the canonical name so it lines up with a real "
                                "harness's composed path"),
        ],
    )
