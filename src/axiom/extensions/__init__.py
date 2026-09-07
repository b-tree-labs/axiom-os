# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Extension system for Neutron OS.

Discovers, loads, and manages user-space extensions from:
  1. .neut/extensions/   (project-local)
  2. ~/.neut/extensions/  (user-level, cross-project)
"""
