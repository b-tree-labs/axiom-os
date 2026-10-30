# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The Data Book: the classification scheme that files every Library item (ADR-213 D11).

The Library is the feature: storage, upload, viewing. The **Data Book** is its
classification scheme, the auto-organizing tree a person browses. Axiom ships a
generic one; a domain contributes its own under its own name (an agricultural
product's "Ag Book", for one), declared as data in the same manifest as its
extractors::

    [[book_schemes]]
    file = "book/my-book.toml"    # or the scheme inline in this table

A scheme is **categories** (a hierarchy of paths, ``logs/by-line``) and
**rules** that place an item from the signals the pipeline has by then: which
extractors ran, the destination tags, the file's name, extension and type, the
extractors' metadata and features, and its text. A rule's ``category`` may be a
template (``measured-data/{metadata.site}``), so a scheme can file "by code" or
"by instrument" without listing every value.

Rules are data, so a domain cannot change how placement works, only what it
knows; every product's Book behaves the same way, and every placement can be
explained in the same words.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UNSORTED = "unsorted"
UNSORTED_LABEL = "Unsorted, needs a look"

_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9{][a-z0-9_.{}-]*)*$")
_TEMPLATE = re.compile(r"\{([a-z_]+)\.([A-Za-z0-9_.-]+)\}")


class SchemeError(ValueError):
    """A scheme that would file things surprisingly if it were accepted."""


@dataclass(frozen=True)
class Category:
    path: str
    label: str
    description: str = ""

    @property
    def parent(self) -> str:
        return self.path.rpartition("/")[0]


@dataclass(frozen=True)
class Rule:
    """``when`` conditions are ANDed; each condition's list is ORed."""

    category: str
    confidence: float
    because: str
    #: Extractor names (globs) that must have produced a result.
    extractor: tuple[str, ...] = ()
    tag: tuple[str, ...] = ()
    ext: tuple[str, ...] = ()
    mime: tuple[str, ...] = ()
    #: File-name globs.
    name: tuple[str, ...] = ()
    #: ``{"code": "widgetsim"}`` (equal) or ``{"code": "*"}`` (present), over merged features.
    feature: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Regular expressions, any of which in the text sample is a match.
    text_any: tuple[str, ...] = ()
    priority: int = 0


@dataclass(frozen=True)
class BookScheme:
    name: str
    display_name: str
    version: str
    categories: tuple[Category, ...]
    rules: tuple[Rule, ...]
    #: Where an item no rule places confidently goes.
    unsorted: str = UNSORTED
    #: Placements below this confidence are marked "needs a look" though filed.
    review_below: float = 0.6
    #: Placements below this go to ``unsorted``.
    unsorted_below: float = 0.35
    source: str = ""

    def __post_init__(self) -> None:
        if not re.match(r"^[a-z0-9_-]+$", self.name or ""):
            raise SchemeError(f"scheme name {self.name!r} must be lower case")
        seen: set[str] = set()
        for c in self.categories:
            if not _PATH_RE.match(c.path) or "{" in c.path:
                raise SchemeError(f"{self.name}: category path {c.path!r} is not a lower-case path")
            if c.path in seen:
                raise SchemeError(f"{self.name}: category {c.path!r} declared twice")
            if c.parent and c.parent not in seen:
                raise SchemeError(f"{self.name}: {c.path!r} is declared before its parent {c.parent!r}")
            seen.add(c.path)
        for r in self.rules:
            static = r.category.split("{", 1)[0].rstrip("/")
            if not _PATH_RE.match(r.category):
                raise SchemeError(f"{self.name}: rule category {r.category!r} is not a path")
            if static and static not in seen:
                raise SchemeError(f"{self.name}: a rule files under {r.category!r}, which no category declares")
            if not 0.0 < r.confidence <= 1.0:
                raise SchemeError(f"{self.name}: a rule's confidence must be in (0, 1]")
            if not r.because:
                raise SchemeError(f"{self.name}: every rule says why ({r.category!r} has no 'because')")

    @property
    def by_path(self) -> dict[str, Category]:
        return {c.path: c for c in self.categories}

    def label_for(self, path: str) -> str:
        if path == self.unsorted:
            return UNSORTED_LABEL
        found = self.by_path.get(path)
        if found:
            return found.label
        return path.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").strip().title() or path

    def labels_for(self, path: str) -> list[str]:
        parts = path.split("/")
        return [self.label_for("/".join(parts[: i + 1])) for i in range(len(parts))]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "version": self.version,
            "unsorted": self.unsorted,
            "categories": [{"path": c.path, "label": c.label, "description": c.description} for c in self.categories],
            "rules": len(self.rules),
            "source": self.source,
        }


def _strs(v: Any) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    return tuple(str(x) for x in v)


def scheme_from_dict(data: dict[str, Any], *, source: str = "") -> BookScheme:
    head = data.get("scheme") or data
    cats = tuple(
        Category(path=str(c["path"]), label=str(c.get("label") or c["path"]), description=str(c.get("description", "")))
        for c in data.get("categories") or []
    )
    rules = []
    for r in data.get("rules") or []:
        when = r.get("when") or {}
        rules.append(
            Rule(
                category=str(r["category"]),
                confidence=float(r.get("confidence", 0.7)),
                because=str(r.get("because", "")),
                extractor=_strs(when.get("extractor")),
                tag=_strs(when.get("tag")),
                ext=tuple(e.lower() for e in _strs(when.get("ext"))),
                mime=_strs(when.get("mime")),
                name=_strs(when.get("name")),
                feature=dict(when.get("feature") or {}),
                metadata=dict(when.get("metadata") or {}),
                text_any=_strs(when.get("text_any")),
                priority=int(r.get("priority", 0)),
            )
        )
    return BookScheme(
        name=str(head.get("name", "")),
        display_name=str(head.get("display_name") or "Data Book"),
        version=str(head.get("version", "1.0.0")),
        categories=cats,
        rules=tuple(rules),
        unsorted=str(head.get("unsorted", UNSORTED)),
        review_below=float(head.get("review_below", 0.6)),
        unsorted_below=float(head.get("unsorted_below", 0.35)),
        source=source,
    )


def load_scheme_file(path: str | Path, *, source: str = "") -> BookScheme:
    return scheme_from_dict(tomllib.loads(Path(path).read_text()), source=source or str(path))


def fill_template(category: str, signals: dict[str, dict[str, Any]]) -> str | None:
    """Resolve ``{features.code}``-style parts; ``None`` when a value is missing."""

    def sub(m: re.Match[str]) -> str:
        group, key = m.group(1), m.group(2)
        value = (signals.get(group) or {}).get(key)
        if value in (None, "", [], {}):
            raise KeyError(key)
        slug = re.sub(r"[^a-z0-9_-]+", "-", str(value).lower()).strip("-")
        if not slug:
            raise KeyError(key)
        return slug[:64]

    try:
        return _TEMPLATE.sub(sub, category)
    except KeyError:
        return None


__all__ = [
    "UNSORTED",
    "UNSORTED_LABEL",
    "BookScheme",
    "Category",
    "Rule",
    "SchemeError",
    "fill_template",
    "load_scheme_file",
    "scheme_from_dict",
]
