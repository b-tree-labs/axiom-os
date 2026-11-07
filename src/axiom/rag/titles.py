# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A document title a reader can read.

A title is taken from a document's first heading or first line. For a screen capture of a
console that line is not a title: fill characters pad its fields, so it arrives as
``04/21/2015DDDDDDDDDDD      STATUS WINDOW  - 2.38    DDDDDDDDDDDDDDDDD11:02``.
:func:`clean_title` drops those runs and folds the padding, and when nothing readable is left
the document's file name stands in, so a citation never shows filler as its name.

Applied where a title is stored (so new ingests keep a clean one) and where it is shown
(so titles already stored read cleanly without a re-ingest).
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

#: Four or more of one non-space character in a row: filler, rules, padding — never a word.
_FILL_RUN = re.compile(r"(\S)\1{3,}")
_SPACE = re.compile(r"\s+")
_ALNUM = re.compile(r"[A-Za-z0-9]")


def clean_title(title: str | None, path: str | None = "") -> str:
    """``title`` with fill runs removed and whitespace folded; the file name if nothing is left.

    An empty title stays empty: what a surface shows for an untitled document is its own call.
    """
    if not (title or "").strip():
        return ""
    text = _SPACE.sub(" ", _FILL_RUN.sub(" ", title or "")).strip()
    if len(_ALNUM.findall(text)) >= 3:
        return text
    name = PurePosixPath((path or "").replace("\\", "/")).name
    return name or text
