# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ModelStrategy — runtime-resolved LLM assembly per spec-model-routing §13.

Strategies decide *which provider plays which role* per step (router /
planner / executor / verifier / embed / rerank). Resolution is gated by
classification, network reachability, cohort policy, budget, and provider
health.

Public surface:
- :mod:`axiom.agents.strategy.types` — primitives (ModelRole, ProviderChoice,
  ResolvedAssembly, ModelContext, etc.).
- :mod:`axiom.agents.strategy.strategy` — ``ModelStrategy`` Protocol.
- :mod:`axiom.agents.strategy.builtin` — built-in strategies
  (legacy-router, cost-conservative, quality-first, cohort-pinned).
"""
