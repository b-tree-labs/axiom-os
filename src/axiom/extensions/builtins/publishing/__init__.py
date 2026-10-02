# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Document lifecycle and publishing.

Houses **PRESS**, the Print agent in the platform's REPL cycle. PRESS owns
the last mile between the system and the human reader: format, render,
publish, and gate. See ``agents/press/persona.md`` for PRESS's role
definition.

The package exposes ``axi pub`` (and the ``axi doc`` alias) for
publishing markdown sources to docx/PDF/LaTeX/slides via Pandoc, with
optional pull-and-reconcile against published copies on OneDrive, Box,
or classroom-scoped shared spaces.
"""
