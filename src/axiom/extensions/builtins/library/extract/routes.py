# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Where a destination tag goes on this install (ADR-213 D7).

An extractor asks for a destination with a tag; a **route** decides. Core
routes every install has:

* ``rag`` — handled by the pipeline itself (the EC hook, then the embed);
* ``dataset`` — tables are written to bronze-derived beside the item, and every
  registered ``dataset`` handler is offered them (a domain's conform lane);
  with none, the dataset is ``held`` as bronze-derived and says so;
* ``training-data``, ``reference-data`` — marked in the catalog for consumers.

``input-deck`` and ``rom-artifact`` have no core handler: a domain registers one
(a model registry, a ROM registry). Without one, the output is ``no-route``:
kept, listed, and picked up when a handler is installed and the item is re-run.

Declared in a manifest::

    [[upload_routes]]
    tag   = "input-deck"
    name  = "mypack.route.model_registry"
    entry = "mypack.routes:register_deck"

A handler is ``handle(ctx: RouteContext) -> RouteOutcome | None``; ``None``
means "not mine" (another pack's tables) and records nothing. It must be
idempotent on ``(item id, extractor name)``: a re-extraction calls it again and
it replaces what it made the last time (ADR-206's rebuild-in-place).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.library import contrib
from axiom.extensions.builtins.library.extract.contract import (
    DESTINATION_TAGS,
    ExtractionResult,
    Registration,
    Table,
)

log = logging.getLogger(__name__)

ROUTE_STATUSES = ("done", "held", "failed", "no-route", "pending")


@dataclass
class RouteContext:
    tag: str
    #: The catalog item as a client sees it (``id``, ``site``, ``owner``, ``file_name``,
    #: ``sha256``, ``size``, ``visibility``, ``source_path``...).
    item: dict[str, Any]
    #: The stored bytes (read-only).
    path: Path
    extractor: str
    extractor_version: str
    result: ExtractionResult
    registration: Registration | None = None
    tables: list[Table] = field(default_factory=list)
    #: Where derived outputs for this item and extractor live in bronze.
    derived_dir: Path | None = None
    #: The archive member these outputs came from, if any.
    member: str = ""


@dataclass
class RouteOutcome:
    status: str
    target: str = ""
    detail: str = ""
    #: ``{"kind": "corral", "label": "...", "ref": "...", "href": "..."}`` for the UI.
    link: dict[str, Any] | None = None
    remedy: str = ""

    def __post_init__(self) -> None:
        if self.status not in ROUTE_STATUSES:
            raise ValueError(f"route status {self.status!r} not among {', '.join(ROUTE_STATUSES)}")

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target": self.target,
            "detail": self.detail,
            "link": self.link,
            "remedy": self.remedy,
        }


Handler = Callable[[RouteContext], "RouteOutcome | None"]


@dataclass
class Route:
    tag: str
    name: str
    handler: Handler | None = None
    entry: str = ""
    source: str = ""

    def resolve(self) -> Handler:
        if self.handler is None:
            self.handler = contrib.resolve_entry(self.entry)
        return self.handler


_lock = threading.Lock()
_local: list[Route] = []


def register_route(tag: str, name: str, handler: Handler) -> None:
    if tag not in DESTINATION_TAGS:
        raise ValueError(f"no destination tag {tag!r}")
    with _lock:
        _local[:] = [r for r in _local if r.name != name]
        _local.append(Route(tag=tag, name=name, handler=handler, source="host"))


def clear_routes() -> None:
    """Tests only."""
    with _lock:
        _local.clear()


def routes_for(tag: str) -> list[Route]:
    found: dict[str, Route] = {}
    for c in contrib.contributions("upload_routes"):
        if c.data.get("tag") == tag and c.data.get("name") and c.data.get("entry"):
            found[c.data["name"]] = Route(tag=tag, name=c.data["name"], entry=c.data["entry"], source=c.source)
    with _lock:
        for r in _local:
            if r.tag == tag:
                found[r.name] = r
    return [found[k] for k in sorted(found)]


__all__ = [
    "ROUTE_STATUSES",
    "Route",
    "RouteContext",
    "RouteOutcome",
    "clear_routes",
    "register_route",
    "routes_for",
]
