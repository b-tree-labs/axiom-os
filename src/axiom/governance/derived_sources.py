# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What an index, a corpus or an embedding job may read: gold, and nothing else (ADR-251 D1).

A derived index is a labelled projection of gold. It never reads the working tier, a person's
unsigned drafts or the tray software drafts into, because none of those has passed the step
that makes a row fit to serve, and an index serves (search, chat, retrieval). This module is the
static guard: :func:`violations` names every reference in a module's code to one of those
places, and :func:`declared_sources_ok` names every declared source that is not a gold object.

A module is an indexer when its file name says so (``index``, ``indexer``, ``corpus``,
``embed``) or when it declares ``INDEX_SOURCES``. Docstrings and comments are prose and are not
scanned; string literals (SQL) and names (tables, models, calls) are.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable

#: The only tier an index reads.
GOLD_PREFIX = "gold."

#: What an index may never read, by kind: the pattern each finding is named by.
FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("silver", re.compile(r"\bsilver\s*\.\s*\w+", re.IGNORECASE)),
    ("drafts", re.compile(r"\b\w*_?drafts?\b|\bAttestDraft\b|\bDraft\b", re.IGNORECASE)),
    ("tray", re.compile(r"\btray\b", re.IGNORECASE)),
)

_NAME = re.compile(r"(^|/)[^/]*(index|indexer|corpus|embed)[^/]*\.py$")
_DECLARES = re.compile(r"^INDEX_SOURCES\s*[:=]", re.MULTILINE)


def is_indexer(path: str, text: str) -> bool:
    """Whether the module at ``path`` (repo-relative) with source ``text`` is an indexer."""
    return bool(_NAME.search(path)) or bool(_DECLARES.search(text))


def _docstrings(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _code_words(text: str) -> Iterable[str]:
    """String literals (not docstrings) and the names a module's code uses."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        yield text  # unparseable: scan it all rather than pass it
        return
    skip = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node.value
        elif isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr
        elif isinstance(node, ast.alias):
            yield node.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def violations(text: str) -> list[str]:
    """``kind: what`` for every forbidden reference in a module's code; empty when clean."""
    found: list[str] = []
    for word in _code_words(text):
        for kind, pattern in FORBIDDEN:
            for m in pattern.finditer(word):
                item = f"{kind}: {m.group(0)}"
                if item not in found:
                    found.append(item)
    return found


def declared_sources_ok(sources: Iterable[str]) -> list[str]:
    """The declared sources that are not gold objects; empty when every one is."""
    return [s for s in sources if not str(s).startswith(GOLD_PREFIX) or violations(repr(str(s)))]


__all__ = ["FORBIDDEN", "GOLD_PREFIX", "declared_sources_ok", "is_indexer", "violations"]
