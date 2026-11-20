# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET — CI/CD and release agent.

RIVET builds, tests, and ships the system. Persona at ``persona.md``;
runtime entrypoints live in the parent ``release/`` package.

TODO(rivet): implement the pipeline-monitoring loop. The persona,
manifest, and ci_monitor stub exist, but the always-on heartbeat that
watches CI pipelines and matches failure patterns is not yet wired into
``axi agents``.
"""
