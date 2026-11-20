# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Choose and run a previewer for one stored file (ADR-213 D12).

Candidates, best first: contributed previewers (``[[previewers]]``) before core
ones (scope first, as with extractors), then priority, then match strength. A
previewer that raises, times out or returns an invalid representation is
skipped and the next is tried; when nothing can show the file, the answer is a
``none`` representation carrying the file's identity and what its extractors
found, which the client draws as the "can't preview" state.

Core previewers, in order: an extractor-supplied representation (an archive's
listing, an HDF5 tree, a series chart), media, tables, JSON/YAML trees, text.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from axiom.extensions.builtins.library import contrib
from axiom.extensions.builtins.library.extract.contract import SCOPE_RANK
from axiom.extensions.builtins.library.preview.contract import (
    MAX_ROWS,
    MAX_TEXT,
    PreviewContext,
    PreviewerManifest,
    RepresentationError,
    previewer_from_dict,
    validate,
)

log = logging.getLogger(__name__)

GUIDE = "docs/guides/build-a-previewer.md"
TREE_BUDGET = 1500


def _cell(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, bool)):
        return v
    return str(v)[:500]


# --------------------------------------------------------------- core previewers


def _extractor_supplied(ctx: PreviewContext) -> dict[str, Any] | None:
    # The file's own results (not an archive member's), most specific scope first.
    ranked = sorted(
        ((name, res) for name, res in ctx.extractions.items() if "!" not in name and res.get("preview")),
        key=lambda kv: (0 if kv[0].startswith("core.") else 1),
        reverse=True,
    )
    for name, res in ranked:
        rep = res["preview"]
        table = _table_from(res)
        if rep.get("type") == "chart" and table is not None:
            return {"type": "composite", "parts": [{"title": "Chart", "preview": rep}, {"title": "Table", "preview": table}]}
        return rep
    return None


def _media(ctx: PreviewContext) -> dict[str, Any] | None:
    mime = ctx.mime or ""
    kind = "pdf" if mime == "application/pdf" else mime.split("/", 1)[0] if mime.split("/", 1)[0] in ("image", "video", "audio") else ""
    if not kind:
        return None
    rep: dict[str, Any] = {"type": "media", "media": kind, "src": "content", "alt": ctx.file_name, "mime": mime}
    for res in ctx.extractions.values():
        meta = res.get("metadata") or {}
        if "width" in meta and "height" in meta:
            rep["width"], rep["height"] = meta["width"], meta["height"]
        if "pages" in meta:
            rep["pages"] = meta["pages"]
    return rep


def _table_from(res: dict[str, Any]) -> dict[str, Any] | None:
    tables = res.get("tables") or []
    if not tables:
        return None

    def one(t: dict[str, Any]) -> dict[str, Any]:
        cols = [{"name": str(c) or f"column {i + 1}", "type": (t.get("types") or {}).get(c, "")} for i, c in enumerate(t["columns"])]
        for c in cols:
            unit = (t.get("units") or {}).get(c["name"])
            if unit:
                c["unit"] = unit
        rows = [[_cell(v) for v in (list(r) + [None] * len(cols))[: len(cols)]] for r in (t.get("rows") or [])[:MAX_ROWS]]
        return {"name": t["name"], "columns": cols, "rows": rows, "row_count": t.get("row_count", len(rows))}

    first = one(tables[0])
    rep: dict[str, Any] = {"type": "table", "columns": first["columns"], "rows": first["rows"], "row_count": first["row_count"]}
    if len(tables) > 1:
        rep["sheets"] = [one(t) for t in tables]
    return rep


def _table(ctx: PreviewContext) -> dict[str, Any] | None:
    for name, res in ctx.extractions.items():
        if "!" in name:
            continue
        rep = _table_from(res)
        if rep is not None:
            return rep
    return None


def _tree_nodes(value: Any, budget: list[int], depth: int = 0) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    items = list(value.items()) if isinstance(value, dict) else list(enumerate(value)) if isinstance(value, list) else []
    for k, v in items[:200]:
        budget[0] -= 1
        if budget[0] <= 0:
            nodes.append({"name": "…", "kind": "more", "detail": "truncated"})
            break
        name = str(k) if isinstance(value, dict) else f"[{k}]"
        if isinstance(v, (dict, list)):
            node = {"name": name, "kind": "object" if isinstance(v, dict) else "array",
                    "detail": f"{len(v)} {'keys' if isinstance(v, dict) else 'items'}"}
            if depth < 8:
                node["children"] = _tree_nodes(v, budget, depth + 1)
            nodes.append(node)
        else:
            nodes.append({"name": name, "kind": "value", "detail": json.dumps(v)[:200] if not isinstance(v, str) else v[:200]})
    if isinstance(value, list) and len(value) > 200:
        nodes.append({"name": "…", "kind": "more", "detail": f"{len(value) - 200} more items"})
    return nodes


def _tree(ctx: PreviewContext) -> dict[str, Any] | None:
    suffix = PurePosixPath(ctx.file_name.lower()).suffix
    if suffix not in (".json", ".yaml", ".yml") or ctx.size > 8 * 1024 * 1024:
        return None
    text = ctx.read_text(8 * 1024 * 1024)
    try:
        if suffix == ".json":
            data = json.loads(text)
        else:
            import yaml

            data = yaml.safe_load(text)
    except ValueError:
        return None  # malformed: the text previewer shows it as it is
    except Exception:  # noqa: BLE001 - yaml's own errors; hostile input declines, never raises
        return None
    return {"type": "tree", "nodes": [{"name": ctx.file_name, "kind": "root", "children": _tree_nodes(data, [TREE_BUDGET])}]}


def _text(ctx: PreviewContext) -> dict[str, Any] | None:
    if (ctx.mime or "").startswith(("image/", "video/", "audio/")) or ctx.mime == "application/pdf":
        return None
    with ctx.path.open("rb") as f:
        head = f.read(MAX_TEXT + 1)
    if b"\x00" in head[:8192]:
        return None
    from axiom.extensions.builtins.library.extract.core.text import language_for

    try:
        text = head[:MAX_TEXT].decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < MAX_TEXT - 4:
            return None
        text = head[: exc.start].decode("utf-8")
    return {"type": "text", "language": language_for(ctx.file_name), "text": text, "truncated": len(head) > MAX_TEXT}


CORE_PREVIEWERS: list[tuple[PreviewerManifest, Callable[[PreviewContext], Any]]] = [
    (PreviewerManifest("core.preview.extracted", "1.0.0", "A representation an extractor supplied",
                       ("chart", "tree", "listing", "composite", "table", "summary"), priority=50, names=("*",)), _extractor_supplied),
    (PreviewerManifest("core.preview.media", "1.0.0", "Images, PDF, video and audio", ("media",), priority=40,
                       mime=("image/*", "video/*", "audio/*", "application/pdf")), _media),
    (PreviewerManifest("core.preview.table", "1.0.0", "Tables: CSV, TSV, Excel, Parquet (first rows, column types)",
                       ("table",), priority=30, names=("*",)), _table),
    (PreviewerManifest("core.preview.tree", "1.0.0", "JSON and YAML as a collapsible tree", ("tree",), priority=20,
                       extensions=(".json", ".yaml", ".yml")), _tree),
    (PreviewerManifest("core.preview.text", "1.0.0", "Text and code, highlighted by language; Markdown rendered",
                       ("text",), priority=10, names=("*",)), _text),
]


# ----------------------------------------------------------------- the registry


def contributed() -> list[tuple[PreviewerManifest, Any]]:
    out = []
    for c in contrib.contributions("previewers"):
        try:
            m = previewer_from_dict(c.data, scope=c.data.get("scope") or ("pack" if c.scope == "core" else c.scope), contributed_by=c.source)
        except ValueError as exc:
            log.warning("library: previewer from %s refused: %s", c.source, exc)
            continue
        out.append((m, None))
    return out


_local: list[tuple[PreviewerManifest, Any]] = []


def register_previewer(manifest: PreviewerManifest | dict[str, Any], fn: Any) -> PreviewerManifest:
    m = manifest if isinstance(manifest, PreviewerManifest) else previewer_from_dict(manifest, scope="pack")
    _local[:] = [(x, f) for x, f in _local if x.name != m.name]
    _local.append((m, fn))
    return m


def clear_previewers() -> None:
    _local.clear()


def describe_all() -> list[dict[str, Any]]:
    return [m.describe() for m, _ in [*CORE_PREVIEWERS, *contributed(), *_local]]


def _run(fn: Callable[[PreviewContext], Any], ctx: PreviewContext, timeout: float) -> Any:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["v"] = fn(ctx)
        except Exception as exc:  # noqa: BLE001
            box["e"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"no preview within {timeout:g}s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def preview_for(path: Path, item: dict[str, Any], extractions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ctx = PreviewContext(path=path, file_name=item["file_name"], size=item["size"], sha256=item["sha256"],
                         mime=item["mime_type"], extractions=extractions)
    names = {n.split("!", 1)[0] for n in extractions}
    candidates = []
    for m, fn in [*_local, *contributed(), *CORE_PREVIEWERS]:
        strength = m.strength(name=ctx.file_name, mime=ctx.mime, extractors=names)
        if strength and ctx.size <= m.max_bytes:
            candidates.append((m, fn, strength))
    candidates.sort(key=lambda c: (-SCOPE_RANK.get(c[0].scope, 0), -c[0].priority, -c[2], c[0].name))
    tried: list[str] = []
    for m, fn, _ in candidates:
        try:
            impl = fn if fn is not None else contrib.resolve_entry(m.entry)
            rep = _run(impl, ctx, m.timeout_s)
            if rep is None:
                continue
            validate(rep, where=m.name)
            return {**rep, "previewer": m.name}
        except (RepresentationError, TimeoutError, Exception) as exc:  # noqa: BLE001 - try the next one
            tried.append(f"{m.name}: {exc}"[:200])
            log.info("library: previewer %s skipped for %s: %s", m.name, ctx.file_name, exc)
    found = [
        {"extractor": n, "summary": r.get("summary", "")}
        for n, r in sorted(extractions.items())
        if r.get("summary") and not n.startswith("core.extract.file_facts")
    ]
    return {
        "type": "none",
        "previewer": "",
        "reason": f"No previewer on this node shows {PurePosixPath(ctx.file_name).suffix or 'this'} files yet.",
        "identity": {"file_name": ctx.file_name, "size": ctx.size, "mime": ctx.mime, "sha256": ctx.sha256},
        "found": found,
        "tried": tried,
        "contribute": {"label": "Contribute a previewer for this type", "guide": GUIDE},
    }


__all__ = ["CORE_PREVIEWERS", "GUIDE", "clear_previewers", "contributed", "describe_all", "preview_for", "register_previewer"]
