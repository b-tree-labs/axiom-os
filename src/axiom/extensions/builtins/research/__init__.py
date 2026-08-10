# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Research / knowledge engine.

Houses **CURIO**, the Eval agent in the platform's REPL cycle. CURIO
runs the compound knowledge loop — discover, read, synthesize, validate,
promote — and serves as the LLM-as-judge scorer in the eval framework.
See ``agents/curio/persona.md`` for CURIO's role definition.

The package exports the corpus-quality primitives:

- ``quality_gate.evaluate_candidate`` — promotion gate for new findings.
- ``confidence_gate.ConfidenceGate`` — RED → YELLOW → GREEN trust
  gradient over the corpus.
- ``chunking_optimizer`` — chunk-strategy selection for ingest.
"""
