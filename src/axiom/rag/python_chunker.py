# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chunking Python source at symbol boundaries instead of by length.

Prose chunkers split on blank lines and headings and cap the result at a
character budget. Applied to code that produces chunks like "the last nine
lines of one function and the first twenty of the next", which retrieve badly
and read worse: the reader gets a body with no name, no signature and no
docstring, and cannot tell what they are looking at.

So a chunk here is a **symbol**: a module, a top-level function, a class, or one
method. It starts at its ``def`` or ``class`` line and ends where the symbol
does, which is where a person would have stopped reading anyway.

Four decisions that are not obvious
-----------------------------------

**Each chunk states what it is.** Retrieval returns text, and a function body
alone is anonymous. Every chunk opens with a header naming the file and the
qualified symbol, so a hit is self-describing even when it is read far from its
source. This costs a couple of lines per chunk and is the difference between an
answer that can be checked and one that has to be trusted.

**A class is split per method, with the class kept as context.** A long class
chunked whole exceeds any sensible budget and then matches every query about
any of its methods — the retrieval equivalent of a file-level answer. Each
method chunk carries the class name and the class docstring's first line, so
the method is still situated.

**A module gets its own chunk**: docstring plus imports plus the names it
defines. "What is this file for" and "what does it depend on" are real
questions, and they are answered by the part of a file that a symbol-only
chunker throws away.

**A file that does not parse is not dropped.** A template, a Python 2
leftover, a partially-written file: the caller gets ``None`` and can fall back
to prose chunking. Losing a whole repository's worth of context because one
file is malformed is a bad trade.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

__all__ = [
    "MAX_SYMBOL_CHARS",
    "PythonSymbol",
    "chunk_python",
    "split_symbols",
]

#: Beyond this, a class is split into its methods rather than kept whole. Not a
#: hard cap on a chunk: a single enormous function is still emitted whole,
#: because half a function is worth less than a long one.
MAX_SYMBOL_CHARS = 4000


@dataclass
class PythonSymbol:
    """One addressable thing in a module."""

    qualname: str
    kind: str  # module | function | class | method
    start_line: int
    end_line: int
    text: str
    docstring: str = ""


def _module_name(rel_path: str) -> str:
    """``pkg/mod.py`` -> ``pkg.mod``; a package init keeps the package name."""
    trimmed = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    parts = [p for p in trimmed.replace("\\", "/").split("/") if p not in ("", ".")]
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _segment(lines: list[str], node: ast.AST) -> str:
    start = getattr(node, "lineno", 1) - 1
    end = getattr(node, "end_lineno", start + 1)
    return "\n".join(lines[start:end])


def _first_line_of(doc: str | None) -> str:
    if not doc:
        return ""
    for line in doc.strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


def _module_overview(
    tree: ast.Module, lines: list[str], module: str
) -> PythonSymbol | None:
    """The module's docstring, its imports, and what it defines.

    Assembled rather than sliced, because those three things are scattered
    through a file and the question they answer — "what is this and what does
    it need" — is asked about the file as a whole.
    """
    doc = ast.get_docstring(tree) or ""
    imports: list[str] = []
    defines: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(_segment(lines, node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defines.append(f"def {node.name}")
        elif isinstance(node, ast.ClassDef):
            defines.append(f"class {node.name}")

    if not (doc or imports or defines):
        # Nothing but a name. An empty ``__init__.py`` is the common case and
        # there are thousands of them; indexing each as "Module pkg.sub" adds
        # rows that can never answer anything and dilute every neighbouring
        # match.
        return None

    body = [f"Module {module}", ""]
    if doc:
        body += [doc.strip(), ""]
    if imports:
        body += ["Imports:", *imports, ""]
    if defines:
        body += ["Defines: " + ", ".join(defines)]
    return PythonSymbol(
        qualname=module,
        kind="module",
        start_line=1,
        end_line=len(lines),
        text="\n".join(body).strip(),
        docstring=doc,
    )


def _class_symbols(
    node: ast.ClassDef, lines: list[str], module: str
) -> list[PythonSymbol]:
    whole = _segment(lines, node)
    doc = ast.get_docstring(node) or ""
    qual = f"{module}.{node.name}"

    methods = [
        child
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if len(whole) <= MAX_SYMBOL_CHARS or not methods:
        return [
            PythonSymbol(
                qualname=qual,
                kind="class",
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                text=whole,
                docstring=doc,
            )
        ]

    # Too long to keep whole. Split per method, and give each one enough of the
    # class to be situated — a method read without its class is a fragment.
    context = f"class {node.name}:"
    summary = _first_line_of(doc)
    if summary:
        context += f"\n    # {summary}"

    symbols = [
        PythonSymbol(
            qualname=qual,
            kind="class",
            start_line=node.lineno,
            end_line=methods[0].lineno - 1,
            text="\n".join(
                [context, "", f"# {len(methods)} methods, chunked separately:"]
                + [f"#   {m.name}" for m in methods]
            ),
            docstring=doc,
        )
    ]
    for method in methods:
        symbols.append(
            PythonSymbol(
                qualname=f"{qual}.{method.name}",
                kind="method",
                start_line=method.lineno,
                end_line=getattr(method, "end_lineno", method.lineno),
                text=f"{context}\n\n{_segment(lines, method)}",
                docstring=ast.get_docstring(method) or "",
            )
        )
    return symbols


def split_symbols(source: str, rel_path: str) -> list[PythonSymbol] | None:
    """Every addressable symbol in ``source``, or ``None`` if it will not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        # Not an error worth failing the file over: a template, a Python 2
        # leftover, something half-written. The caller falls back to prose.
        return None

    lines = source.splitlines()
    module = _module_name(rel_path)
    overview = _module_overview(tree, lines, module)
    symbols = [overview] if overview is not None else []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                PythonSymbol(
                    qualname=f"{module}.{node.name}",
                    kind="function",
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    text=_segment(lines, node),
                    docstring=ast.get_docstring(node) or "",
                )
            )
        elif isinstance(node, ast.ClassDef):
            symbols.extend(_class_symbols(node, lines, module))

    return symbols


def chunk_python(content: str, rel_path: str):
    """Chunk Python source at symbol boundaries.

    Returns ``None`` when the source will not parse, so the caller can fall
    back rather than lose the file.
    """
    from .chunker import Chunk

    symbols = split_symbols(content, rel_path)
    if symbols is None:
        return None

    chunks = []
    for index, symbol in enumerate(symbols):
        if not symbol.text.strip():
            continue
        # The header is why a hit is self-describing. Retrieval returns text,
        # and a body with no name cannot be checked against its source.
        header = f"# {rel_path}:{symbol.start_line} — {symbol.kind} {symbol.qualname}"
        chunks.append(
            Chunk(
                text=f"{header}\n{symbol.text}",
                source_path=rel_path,
                source_title=symbol.qualname,
                chunk_index=index,
                start_line=symbol.start_line,
                source_type="python",
            )
        )
    return chunks
