# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Universal facts, plain text, Markdown, source code, JSON, YAML and XML."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

from axiom.extensions.builtins.library.extract.contract import (
    ExtractionContext,
    ExtractionResult,
    TextSection,
)

#: Text bound for retrieval is capped: past this a file is data, not prose, and
#: the embed path's own chunk ceiling would refuse it anyway.
MAX_RAG_TEXT = 4 * 1024 * 1024

LANGUAGES = {
    ".md": "markdown", ".markdown": "markdown", ".rst": "rst", ".txt": "text", ".log": "text",
    ".py": "python", ".ipynb": "json", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
    ".f": "fortran", ".f90": "fortran", ".f95": "fortran", ".for": "fortran", ".jl": "julia",
    ".r": "r", ".m": "matlab", ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".sql": "sql",
    ".rs": "rust", ".go": "go", ".java": "java", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".xml": "xml", ".tex": "latex", ".csv": "csv",
    ".tsv": "csv",
}
CODE_EXTENSIONS = frozenset(
    e for e, lang in LANGUAGES.items()
    if lang not in ("markdown", "rst", "text", "json", "yaml", "xml", "csv", "toml", "ini", "latex")
)


def language_for(name: str) -> str:
    return LANGUAGES.get(PurePosixPath(name.lower()).suffix, "text")


def facts(ctx: ExtractionContext) -> ExtractionResult:
    """What every file has: size, checksum, type, and whether it reads as text."""
    text = ctx.head_text
    meta: dict[str, Any] = {
        "file_name": ctx.file_name,
        "size": ctx.size,
        "sha256": ctx.sha256,
        "mime": ctx.mime,
        "extension": PurePosixPath(ctx.file_name.lower()).suffix,
        "encoding": "utf-8" if text is not None else "binary",
    }
    if ctx.member:
        meta["member"] = ctx.member
    return ExtractionResult(metadata=meta, summary=f"{ctx.size:,} bytes, {meta['encoding']}")


def _md_headings(text: str) -> list[str]:
    return [m.group(2).strip() for m in re.finditer(r"^(#{1,3})\s+(.+)$", text, re.M)][:50]


def plain_text(ctx: ExtractionContext) -> ExtractionResult:
    """Plain text, Markdown, reStructuredText, logs and source code."""
    raw = ctx.read_bytes(MAX_RAG_TEXT + 1)
    text = raw[:MAX_RAG_TEXT].decode("utf-8", errors="replace")
    lang = language_for(ctx.file_name)
    lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    meta: dict[str, Any] = {"language": lang, "lines": lines, "chars": len(text)}
    features: dict[str, Any] = {"language": lang}
    title = ""
    if lang == "markdown":
        heads = _md_headings(text)
        meta["headings"] = heads
        title = heads[0] if heads else ""
    elif lang == "rst":
        m = re.search(r"^(.+)\n[=\-~^]{3,}\s*$", text, re.M)
        title = m.group(1).strip() if m else ""
    if PurePosixPath(ctx.file_name.lower()).suffix in CODE_EXTENSIONS:
        features["code_like"] = True
        defs = re.findall(r"^\s*(?:def|class|function|subroutine|program|module)\s+([A-Za-z_]\w*)", text, re.M | re.I)
        meta["definitions"] = defs[:100]
    if title:
        meta["title"] = title
    warnings = []
    if len(raw) > MAX_RAG_TEXT:
        warnings.append(f"only the first {MAX_RAG_TEXT // (1024 * 1024)} MB is indexed")
    summary = f"{lang} text, {lines:,} lines" + (f": {title}" if title else "")
    return ExtractionResult(
        metadata=meta, features=features, text=[TextSection(text)] if text.strip() else [],
        summary=summary, warnings=warnings,
    )


def _outline(value: Any, depth: int = 0, budget: list[int] | None = None) -> Any:
    """A bounded outline of a JSON-like value: keys and types, sample scalars."""
    budget = budget if budget is not None else [400]
    budget[0] -= 1
    if budget[0] <= 0 or depth > 6:
        return "…"
    if isinstance(value, dict):
        return {str(k): _outline(v, depth + 1, budget) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        if not value:
            return []
        return [_outline(value[0], depth + 1, budget), f"… {len(value)} items"] if len(value) > 1 else [_outline(value[0], depth + 1, budget)]
    if isinstance(value, str):
        return value[:80]
    return value


def structured(ctx: ExtractionContext) -> ExtractionResult:
    """JSON and YAML: their structure, top-level keys, and the text for retrieval."""
    raw = ctx.read_bytes(64 * 1024 * 1024 + 1)
    text = raw.decode("utf-8", errors="replace")
    suffix = PurePosixPath(ctx.file_name.lower()).suffix
    fmt = "yaml" if suffix in (".yaml", ".yml") else "json"
    data: Any
    if fmt == "json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            # JSON lines is common for data; take the first record as the shape.
            try:
                first = next(line for line in text.splitlines() if line.strip())
                data = [json.loads(first)]
                fmt = "jsonl"
            except Exception:
                raise ValueError(f"not valid JSON: {exc.msg} at line {exc.lineno}") from None
    else:
        import yaml

        data = yaml.safe_load(text)
    meta: dict[str, Any] = {"format": fmt, "root": type(data).__name__}
    if isinstance(data, dict):
        meta["keys"] = [str(k) for k in list(data)[:100]]
    elif isinstance(data, list):
        meta["items"] = len(data)
    sections = [TextSection(text[:MAX_RAG_TEXT])] if len(raw) <= 2 * 1024 * 1024 else []
    return ExtractionResult(
        metadata=meta,
        features={"structure": _outline(data)},
        text=sections,
        summary=f"{fmt.upper()} {meta['root']}" + (f" with {len(meta.get('keys', []))} keys" if "keys" in meta else ""),
    )


def xml(ctx: ExtractionContext) -> ExtractionResult:
    """XML: root element, namespaces, element counts; parsed without entity expansion."""
    from xml.etree.ElementTree import XMLParser  # noqa: S405 - entities refused below

    raw = ctx.read_bytes(64 * 1024 * 1024 + 1)
    if b"<!ENTITY" in raw[:65536] or b"<!DOCTYPE" in raw[:65536] and b"SYSTEM" in raw[:65536]:
        raise ValueError("XML declares entities or an external DTD; not parsed (entity expansion is refused)")
    counts: dict[str, int] = {}
    root_tag = ""

    class Target:
        depth = 0

        def start(self, tag: str, attrib: dict[str, str]) -> None:
            nonlocal root_tag
            local = tag.rsplit("}", 1)[-1]
            if not root_tag:
                root_tag = local
            counts[local] = counts.get(local, 0) + 1

        def end(self, tag: str) -> None:
            pass

        def data(self, data: str) -> None:
            pass

        def close(self) -> None:
            pass

    parser = XMLParser(target=Target())  # noqa: S314 - no entity expansion, DTDs refused above
    parser.feed(raw)
    parser.close()
    text = raw.decode("utf-8", errors="replace")
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain).strip()
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:30]
    return ExtractionResult(
        metadata={"root": root_tag, "elements": dict(top)},
        features={"xml_root": root_tag},
        text=[TextSection(plain[:MAX_RAG_TEXT])] if plain and len(raw) <= 2 * 1024 * 1024 else [],
        summary=f"XML <{root_tag}>, {sum(counts.values()):,} elements",
    )


__all__ = ["CODE_EXTENSIONS", "LANGUAGES", "facts", "language_for", "plain_text", "structured", "xml"]
