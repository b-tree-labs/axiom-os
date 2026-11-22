# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TRIAGE — the diagnostics + security agent.

Persona lives in ``persona.md`` next to this file — it is loaded into
the agent's identity layer at runtime via
:func:`axiom.agents.skills_runtime.weave_agent_skills` (per the
Axiomatic Way principle #7).

Agent code is in the parent package (``..agent``, ``..reviewer``,
``..subscriber``, ``..tools``) during this transitional phase; a
future refactor may move those into this directory to better
reflect AEOS §4.1 layout. That refactor is deferred so the first
migration stays tight.
"""
