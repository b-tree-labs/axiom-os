# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Drop-in tool extension directory for neut chat.

Place self-contained tool modules here. Each module exports:
  TOOLS: list[ToolDef]       — tool definitions
  execute(name, params) -> dict  — handler function

The registry auto-discovers modules in this directory on each turn.
"""
