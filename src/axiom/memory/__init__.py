# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom memory subsystem.

Core primitives:
- `fragment`: MemoryFragment with immutable (T,U,A,R) provenance
  and MIRIX 6-manager cognitive-type taxonomy.

Federation, access control, retention, signing, and policy layers
build on top — see axiom/memory/ other modules and
project_memory_architecture_unified.md.
"""
