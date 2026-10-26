# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Presenters: the statement as a person sees or hears it (ADR-144).

A presenter renders a statement's content at four densities (``strip``,
``line``, ``card``, ``page``) and as speakable text, and lists the fields a
person may correct. Every presentation stores the presenter's id and version
with the text it produced, so the record shows exactly what was put in front
of the signer.

Each entry type gets a presenter built from its logbook declaration. An extension
may register its own for a type. Presenters only format what the statement
contains; they never fetch or add a value.

Speakable text uses a small markup a speech engine expands: ``<digits>`` reads
a number digit by digit, which is how identifiers and readings are read back
("one two zero seven", "four point two"). Content is escaped, so a value can
never inject markup.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .logbooks import EntryType

DENSITIES = ("strip", "line", "card", "page")

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class FieldRef:
    id: str
    label: str
    type: str


class Presenter(Protocol):
    id: str
    version: str

    def render(self, content: dict[str, Any], density: str) -> str: ...

    def speakable(self, content: dict[str, Any]) -> str: ...

    def correctable_fields(self, content: dict[str, Any]) -> list[FieldRef]: ...


def _value(v: Any) -> str:
    if v is True:
        return "yes"
    if v is False:
        return "no"
    if v is None:
        return "not entered"
    if isinstance(v, dict) and set(v) == {"value", "unit"}:
        # A quantity: the value with its unit, as the person entered it.
        return f"{v['value']} {v['unit']}".rstrip()
    return str(v)


def _say_numbers(text: str) -> str:
    def one(m: re.Match[str]) -> str:
        whole, _, frac = m.group(0).partition(".")
        spoken = f"<digits>{whole}</digits>"
        if frac:
            spoken += f" point <digits>{frac}</digits>"
        return spoken

    return _NUMBER.sub(one, text)


class LogbookPresenter:
    """The default presenter for an entry type, built from its declaration."""

    def __init__(self, et: EntryType, logbook_version: str, site_id: str = "") -> None:
        self._et = et
        self._site = site_id
        self.id = f"{et.logbook_id}.{et.id.lower()}"
        self.version = logbook_version

    def _lines(self, content: dict[str, Any]) -> list[str]:
        fields = content.get("fields") or {}
        lines: list[str] = []
        for f in self._et.fields:
            if f.id not in fields:
                continue
            if f.type == "readings" and isinstance(fields[f.id], dict):
                labels = self._instrument_labels(f)
                for inst_id, r in fields[f.id].items():
                    if isinstance(r, dict):
                        lines.append(
                            f"{labels.get(inst_id, inst_id)}: {r.get('value')} {r.get('unit', '')}".rstrip()
                        )
            else:
                lines.append(f"{f.label or f.id}: {_value(fields[f.id])}")
        return lines

    def _instrument_labels(self, f: Any) -> dict[str, str]:
        from . import field_sources

        if not self._site or not f.from_site:
            return {}
        try:
            return {i.id: i.label for i in field_sources.resolve(f.from_site, self._site)}
        except LookupError:
            return {}

    def render(self, content: dict[str, Any], density: str) -> str:
        if density not in DENSITIES:
            raise ValueError(f"unknown density {density!r}; one of {DENSITIES}")
        title = str(content.get("title", ""))
        if density == "strip":
            return title
        if density == "line":
            return f"{self._et.id} · {title}"
        lines = [f"{self._et.id} · {title}", *(f"  {x}" for x in self._lines(content))]
        if density == "page" and content.get("body"):
            lines += ["", str(content["body"])]
        return "\n".join(lines)

    def speakable(self, content: dict[str, Any]) -> str:
        parts = [str(content.get("title", "")), *self._lines(content)]
        return ". ".join(_say_numbers(html.escape(p, quote=False)) for p in parts if p) + "."

    def correctable_fields(self, content: dict[str, Any]) -> list[FieldRef]:
        return [FieldRef(f.id, f.label or f.id, f.type) for f in self._et.fields]


_registered: dict[tuple[str, str], Presenter] = {}


def register(logbook: str, entry_type: str, presenter: Presenter) -> None:
    """Use ``presenter`` for one entry type instead of the default."""
    _registered[(logbook, entry_type)] = presenter


def unregister(logbook: str, entry_type: str) -> None:
    _registered.pop((logbook, entry_type), None)


def for_type(et: EntryType, *, logbook_version: str, site_id: str = "") -> Presenter:
    return _registered.get((et.logbook_id, et.id)) or LogbookPresenter(et, logbook_version, site_id)


__all__ = [
    "LogbookPresenter",
    "DENSITIES",
    "FieldRef",
    "Presenter",
    "for_type",
    "register",
    "unregister",
]
