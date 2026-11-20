# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext search <query>`` — substring match across registry metadata.

We query the local-filesystem registry backend (or a ``--registry file://``
override). The search is intentionally naive — case-insensitive substring
over the extension's name, description, and declared tags. No fuzzy
matching, no ranking, no remote calls.

The manifest fields we consult come from the latest published version of
each extension:

- ``[extension].description``
- ``[extension.tags]`` — optional, list of strings

An empty result is *not* a failure: we print a one-line "no matches" note
to stderr and exit 0 so callers can pipe the output without special-casing
it.
"""

from __future__ import annotations

import argparse
import json
import os
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom.cli.ext._output import console, error_console, next_steps
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.registry_backend import (
    RegistryPath,
    list_extensions,
)
from axiom.cli.ext.registry_backend import (
    get as registry_get,
)


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


@dataclass(frozen=True)
class SearchHit:
    """A single match row."""

    name: str
    latest: str
    description: str
    tags: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "latest": self.latest,
            "description": self.description,
            "tags": list(self.tags),
        }


def _load_manifest_fields(manifest_path: Path) -> tuple[str, tuple[str, ...]]:
    """Return ``(description, tags)`` for a manifest, safely."""
    try:
        with manifest_path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:  # noqa: BLE001 — best-effort; broken manifest = empty
        return "", ()
    ext = data.get("extension", {}) or {}
    description = str(ext.get("description", "") or "")
    raw_tags = ext.get("tags", ()) or ()
    if not isinstance(raw_tags, (list, tuple)):
        raw_tags = ()
    tags = tuple(str(t) for t in raw_tags if isinstance(t, (str, int, float)))
    return description, tags


def _latest_version(name: str) -> str:
    """Return the latest indexed version for ``name`` or empty string."""
    from axiom.cli.ext.registry_backend import read_index

    idx = read_index()
    entry = (idx.get("extensions") or {}).get(name) or {}
    return str(entry.get("latest") or "")


def search_registry(query: str) -> list[SearchHit]:
    """Return every registered extension whose name/description/tags match.

    Empty ``query`` matches everything — the CLI layer forbids that but we
    keep the function tolerant so callers driving it programmatically
    (search every extension) don't have to special-case it.
    """
    q = query.lower()
    hits: list[SearchHit] = []
    for name in list_extensions():
        latest = _latest_version(name)
        record = registry_get(name, latest) if latest else None
        description, tags = ("", ())
        if record is not None:
            description, tags = _load_manifest_fields(record.manifest_path)

        haystack_parts: list[str] = [name, description]
        haystack_parts.extend(tags)
        haystack = "\n".join(p.lower() for p in haystack_parts if p)
        if not q or q in haystack:
            hits.append(
                SearchHit(
                    name=name,
                    latest=latest,
                    description=description,
                    tags=tags,
                )
            )
    return hits


def _format_table(hits: Iterable[SearchHit]) -> str:
    rows = list(hits)
    if not rows:
        return ""
    # A registry description is arbitrary length and was appended after the
    # padding with nothing to catch it, so a search result ran off the edge.
    from axiom.infra.cli_format import SQUARE, Column, table, terminal_width

    return "\n".join(
        table(
            [(r.name, r.latest or "-", r.description) for r in rows],
            [Column("NAME"), Column("LATEST"), Column("DESCRIPTION", wrap=True)],
            width=terminal_width(reserve=2),
            headers=True,
            border=SQUARE,
        )
    )


class SearchProvider:
    """Built-in provider for ``axi ext search <query>``."""

    verb = "search"
    description = "Search the registry for extensions by name/description/tag"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "query",
            help="Case-insensitive substring to match against name/description/tags",
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit results as JSON",
        )
        parser.add_argument(
            "--registry",
            dest="registry_override",
            default=None,
            help="Override the registry URL (file:// only at v0.1)",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        if args.registry_override is not None:
            if not args.registry_override.startswith("file://"):
                print(
                    f"{_brand_cli()} ext search: --registry must use the file:// scheme "
                    f"(got {args.registry_override!r}); remote registries are "
                    "a later provider override."
                )
                return 1
            os.environ["AXIOM_REGISTRY_URL"] = args.registry_override

        query = args.query or ""
        if not query.strip():
            print(f"{_brand_cli()} ext search: query cannot be empty")
            return 2

        try:
            hits = search_registry(query)
        except ValueError as exc:
            print(f"{_brand_cli()} ext search: {exc}")
            return 1

        if args.as_json:
            print(
                json.dumps(
                    {"query": query, "hits": [h.to_json() for h in hits]},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if not hits:
            # Empty results go to stderr; exit 0 so pipes don't trip.
            err_con = error_console()
            err_con.print(f"no extensions match {query!r}")
            # Also print a pointer at where we looked so users can sanity-check.
            err_con.print(f"  registry: file://{RegistryPath.resolve().root}")
            return 0

        con = console()
        con.print(_format_table(hits))
        con.print("")
        # Surface the natural next verbs — the first hit is the most likely target.
        first = hits[0].name
        next_steps(
            [
                f"{_brand_cli()} ext show {first}       # Inspect details",
                f"{_brand_cli()} ext install {first}    # Install the latest",
            ]
        )
        return 0


__all__ = ["SearchHit", "SearchProvider", "search_registry"]
