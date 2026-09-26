# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Skill functions for the ADR-065 schema-bilingual config primitive.

Three verbs (validate / show / emit-schema) registered through
``axiom.infra.skills.SkillRegistry`` and surfaced as ``axi config``
subcommands. Per ADR-056, CLI handlers are thin wrappers over these
``(params, ctx) -> SkillResult`` functions.
"""
