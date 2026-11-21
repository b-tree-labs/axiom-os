# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axi commands — cross-harness slash-command generator.

Discovers all installed Axiom extensions' CLI verbs and chat slash commands,
resolves conflicts deterministically (builtin < user < project; alphabetical
within tier), and emits per-harness shims so users can invoke the same
operations via Claude Code / Cursor / Codex with their native UX.

Single source of truth for nouns: AEOS `[[extension.provides]] kind = "cmd"`
blocks across all installed extensions, dynamically rolled up at generate-time.
"""
