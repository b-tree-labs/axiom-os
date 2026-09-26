# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Converter providers for Publisher — multi-format document conversion.

All converters inherit from BaseConverter and implement a common interface
for converting between formats (docx, markdown, latex, html, etc.) while
preserving document structure, images, and metadata.

Registry:
    pandoc.py — .docx ↔ .md, .latex ↔ .md, .html ↔ .md via pandoc
    (future: latex.py, html.py, native implementations)
"""
