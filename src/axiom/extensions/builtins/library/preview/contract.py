# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The previewer contract (ADR-213 D12): how a stored file is shown in the Library and chat.

A previewer does not draw. It returns a **representation**: a small, typed,
plain-data document that the client renders with Axiom's own components (a
table, a tree, highlighted text, the platform chart). Contributed code therefore
never runs in the viewer's page: a domain pack's previewer is Python on the
node that returns data, and the browser draws that data with code it already
trusts. That is the main security property of previews, and it is why there is
one representation vocabulary rather than a plugin API for the browser.

Representation types (``type``):

============  ===============================================================
``media``     ``media`` is ``image`` / ``pdf`` / ``video`` / ``audio``; the client
              loads the item's own content URL. ``alt`` is required.
``text``      ``text`` plus ``language`` (``markdown``, ``python``, ``json``, a
              domain grammar a pack names) for highlighting. Shown as text,
              never as markup; Markdown is rendered sanitized.
``table``     ``columns`` (``name``, ``type``, optional ``unit``), ``rows`` (the
              first N), ``row_count``; optional ``sheets`` for workbooks.
``tree``      ``nodes``: ``{name, kind, detail?, children?, peek?}``. JSON/YAML,
              HDF5/NetCDF structure, with small dataset peeks.
``listing``   ``entries``: ``{path, size, kind}``. Archives.
``chart``     ``title``, ``x`` and ``y`` axes (``label``, ``unit``, ``scale``),
              ``series`` (``name``, ``unit``, ``points`` as ``[x, y]``,
              ``kind`` ``line``/``step``/``bar``/``markers``). Optionally a
              ``chart_spec`` (a stored-channel ChartSpec) or a ``plotly`` figure
              for the Trame/Plotly translation path. Rendered by the Charts
              component, with "Open in Charts".
``summary``   ``fields``: ``{label, value, unit?}`` and optional ``tables``.
``composite`` ``parts``: ``{title, preview}``, each a representation.
``html``      ``html``, sanitized on the node and rendered only in a sandboxed
              iframe without same-origin. For rich output nothing else fits.
``none``      Nothing can show this file. ``reason``; the client shows the
              file's identity, Download, Open with, and "Contribute a previewer".
============  ===============================================================

A previewer's manifest (``[[previewers]]``)::

    [[previewers]]
    name     = "mypack.preview.widget_deck"   # <scope>.preview.<name>
    version  = "1.0.0"
    summary  = "A widget deck as highlighted text with a summary panel"
    entry    = "mypack.previewers:widget_deck"
    produces = ["composite"]
    priority = 50
    [previewers.match]
    extractors = ["mypack.extract.widget_deck"]   # extractor results it reads
    extensions = [".i", ".inp"]
    mime       = ["text/*"]
    [previewers.limits]
    timeout_s = 5
    max_bytes = 20_000_000
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

REPRESENTATION_TYPES = ("media", "text", "table", "tree", "listing", "chart", "summary", "composite", "html", "none")
MEDIA_KINDS = ("image", "pdf", "video", "audio")
SERIES_KINDS = ("line", "step", "bar", "markers")
SCALES = ("linear", "log", "time")

#: The most rows, nodes, points and characters a representation carries. A
#: preview is a look, not a download; the bounds are what keep it cheap.
MAX_ROWS = 200
MAX_NODES = 2000
MAX_POINTS = 20_000
MAX_TEXT = 512 * 1024

_NAME_RE = re.compile(r"^[a-z0-9_]+\.preview\.[a-z0-9_]+$")


class RepresentationError(ValueError):
    """A representation the client could not render safely or accessibly."""


@dataclass(frozen=True)
class PreviewerManifest:
    name: str
    version: str
    summary: str
    produces: tuple[str, ...]
    entry: str = ""
    priority: int = 0
    scope: str = "core"
    contributed_by: str = ""
    extractors: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    mime: tuple[str, ...] = ()
    timeout_s: float = 5.0
    max_bytes: int = 200 * 1024 * 1024

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"previewer name {self.name!r} must be <scope>.preview.<name>")
        bad = sorted(set(self.produces) - set(REPRESENTATION_TYPES))
        if bad or not self.produces:
            raise ValueError(f"{self.name}: produces {bad or '(none)'} not among {', '.join(REPRESENTATION_TYPES)}")
        if not (self.extractors or self.extensions or self.names or self.mime):
            raise ValueError(f"{self.name}: a previewer needs at least one match signal")

    def strength(self, *, name: str, mime: str, extractors: set[str]) -> int:
        best = 0
        if self.mime and mime and any(fnmatch.fnmatch(mime, m) for m in self.mime):
            best = 1
        if self.extensions and PurePosixPath(name.lower()).suffix in self.extensions:
            best = max(best, 2)
        if self.names and any(fnmatch.fnmatch(name.lower(), p.lower()) for p in self.names):
            best = max(best, 3)
        if self.extractors and any(fnmatch.fnmatch(e, p) for e in extractors for p in self.extractors):
            best = max(best, 4)
        return best

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "produces": list(self.produces),
            "priority": self.priority,
            "scope": self.scope,
            "contributed_by": self.contributed_by,
            "match": {
                "extractors": list(self.extractors),
                "extensions": list(self.extensions),
                "names": list(self.names),
                "mime": list(self.mime),
            },
        }


def _strs(v: Any) -> tuple[str, ...]:
    if v is None:
        return ()
    return (v,) if isinstance(v, str) else tuple(str(x) for x in v)


def previewer_from_dict(data: dict[str, Any], *, scope: str = "", contributed_by: str = "") -> PreviewerManifest:
    m = data.get("match") or {}
    lim = data.get("limits") or {}
    return PreviewerManifest(
        name=str(data.get("name", "")),
        version=str(data.get("version", "")),
        summary=str(data.get("summary", "")),
        produces=_strs(data.get("produces")),
        entry=str(data.get("entry", "")),
        priority=int(data.get("priority", 0)),
        scope=scope or str(data.get("scope", "core")),
        contributed_by=contributed_by,
        extractors=_strs(m.get("extractors")),
        extensions=tuple(e.lower() for e in _strs(m.get("extensions"))),
        names=_strs(m.get("names")),
        mime=_strs(m.get("mime")),
        timeout_s=float(lim.get("timeout_s", 5.0)),
        max_bytes=int(lim.get("max_bytes", 200 * 1024 * 1024)),
    )


@dataclass
class PreviewContext:
    """One stored file and what the extractors found in it."""

    path: Path
    file_name: str
    size: int
    sha256: str
    mime: str
    #: Extractor name to its stored result (``ExtractionResult.to_json()``).
    extractions: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_rows: int = 50

    def read_text(self, limit: int = MAX_TEXT) -> str:
        with self.path.open("rb") as f:
            return f.read(limit).decode("utf-8", errors="replace")


class Previewer(Protocol):
    def __call__(self, ctx: PreviewContext) -> dict[str, Any] | None: ...


# ------------------------------------------------------------------ validation

_COLOR_KEYS = {"color", "colour", "fill", "stroke", "background", "bg"}
_TOKEN = re.compile(r"^(var\(--[a-z0-9-]+\)|series-\d+|accent|muted|positive|negative|warning)$")
_SCRIPTY = re.compile(r"<\s*script|javascript:|\bon[a-z]+\s*=|<\s*iframe|<\s*object|<\s*embed", re.I)


def validate(rep: Any, *, where: str = "preview") -> None:
    """Refuse a representation the client could not render safely, accessibly or in both themes."""
    if not isinstance(rep, dict):
        raise RepresentationError(f"{where}: a representation is a mapping")
    kind = rep.get("type")
    if kind not in REPRESENTATION_TYPES:
        raise RepresentationError(f"{where}: type {kind!r} not among {', '.join(REPRESENTATION_TYPES)}")
    _no_raw_colours(rep, where)
    if kind == "media":
        if rep.get("media") not in MEDIA_KINDS:
            raise RepresentationError(f"{where}: media must be one of {', '.join(MEDIA_KINDS)}")
        if not str(rep.get("alt") or "").strip():
            raise RepresentationError(f"{where}: media needs alt text")
        if "src" in rep and str(rep["src"]) not in ("content",):
            raise RepresentationError(f"{where}: media loads the item's own content, never a URL ({rep['src']!r})")
    elif kind == "text":
        if not isinstance(rep.get("text"), str):
            raise RepresentationError(f"{where}: text needs a text string")
        if len(rep["text"]) > MAX_TEXT:
            raise RepresentationError(f"{where}: text over {MAX_TEXT} characters; truncate and set truncated")
    elif kind == "table":
        cols = rep.get("columns")
        if not isinstance(cols, list) or not cols or not all(isinstance(c, dict) and str(c.get("name", "")).strip() for c in cols):
            raise RepresentationError(f"{where}: every table column needs a name (it is the header a reader hears)")
        rows = rep.get("rows") or []
        if len(rows) > MAX_ROWS:
            raise RepresentationError(f"{where}: at most {MAX_ROWS} rows in a preview")
        if any(not isinstance(r, list) or len(r) != len(cols) for r in rows):
            raise RepresentationError(f"{where}: every row has one cell per column")
    elif kind == "tree":
        count = _count_nodes(rep.get("nodes"), where)
        if count > MAX_NODES:
            raise RepresentationError(f"{where}: at most {MAX_NODES} tree nodes")
    elif kind == "listing":
        entries = rep.get("entries")
        if not isinstance(entries, list) or any(not str(e.get("path", "")) for e in entries):
            raise RepresentationError(f"{where}: every listing entry needs a path")
    elif kind == "chart":
        if not str(rep.get("title") or "").strip():
            raise RepresentationError(f"{where}: a chart needs a title")
        for axis in ("x", "y"):
            a = rep.get(axis) or {}
            if not str(a.get("label") or "").strip():
                raise RepresentationError(f"{where}: the {axis} axis needs a label")
            if a.get("scale", "linear") not in SCALES:
                raise RepresentationError(f"{where}: {axis} scale must be one of {', '.join(SCALES)}")
        series = rep.get("series") or []
        if not series and not rep.get("chart_spec") and not rep.get("plotly"):
            raise RepresentationError(f"{where}: a chart needs series, a chart_spec or a plotly figure")
        points = 0
        for s in series:
            if not str(s.get("name") or "").strip():
                raise RepresentationError(f"{where}: every series needs a name (the legend is how it is read)")
            if s.get("kind", "line") not in SERIES_KINDS:
                raise RepresentationError(f"{where}: series kind must be one of {', '.join(SERIES_KINDS)}")
            points += len(s.get("points") or [])
        if points > MAX_POINTS:
            raise RepresentationError(f"{where}: at most {MAX_POINTS} points; decimate first")
    elif kind == "summary":
        for f in rep.get("fields") or []:
            if not str(f.get("label") or "").strip():
                raise RepresentationError(f"{where}: every summary field needs a label")
        for i, t in enumerate(rep.get("tables") or []):
            validate({"type": "table", **t}, where=f"{where}.tables[{i}]")
    elif kind == "composite":
        parts = rep.get("parts")
        if not isinstance(parts, list) or not parts:
            raise RepresentationError(f"{where}: a composite needs parts")
        for i, p in enumerate(parts):
            if not str(p.get("title") or "").strip():
                raise RepresentationError(f"{where}: every composite part needs a title")
            validate(p.get("preview"), where=f"{where}.parts[{i}]")
    elif kind == "html":
        html = rep.get("html")
        if not isinstance(html, str):
            raise RepresentationError(f"{where}: html needs an html string")
        if _SCRIPTY.search(html):
            raise RepresentationError(f"{where}: html carries script; sanitize it (preview.sanitize.clean_html)")
        if not str(rep.get("title") or "").strip():
            raise RepresentationError(f"{where}: html needs a title (the frame's accessible name)")
    elif kind == "none":
        if not str(rep.get("reason") or "").strip():
            raise RepresentationError(f"{where}: say why nothing can show this file")


def _count_nodes(nodes: Any, where: str) -> int:
    if nodes is None:
        return 0
    if not isinstance(nodes, list):
        raise RepresentationError(f"{where}: tree nodes are a list")
    n = 0
    for node in nodes:
        if not isinstance(node, dict) or not str(node.get("name", "")).strip():
            raise RepresentationError(f"{where}: every tree node needs a name")
        n += 1 + _count_nodes(node.get("children"), where)
    return n


def _no_raw_colours(value: Any, where: str) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in _COLOR_KEYS and isinstance(v, str) and not _TOKEN.match(v):
                raise RepresentationError(
                    f"{where}: {k}={v!r} is a raw colour; use a theme token (var(--...), series-N) so it works in light and dark"
                )
            if k == "plotly":
                continue  # the Plotly path is re-themed by the importer
            _no_raw_colours(v, where)
    elif isinstance(value, list):
        for v in value:
            _no_raw_colours(v, where)


__all__ = [
    "MAX_NODES",
    "MAX_POINTS",
    "MAX_ROWS",
    "MAX_TEXT",
    "REPRESENTATION_TYPES",
    "PreviewContext",
    "Previewer",
    "PreviewerManifest",
    "RepresentationError",
    "previewer_from_dict",
    "validate",
]
