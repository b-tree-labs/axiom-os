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
                    if isinstance(r, dict) and r.get("not_taken"):
                        # Said in words, never as an empty value (#1809).
                        lines.append(f"{labels.get(inst_id, inst_id)}: not taken ({r.get('reason')})")
                    elif isinstance(r, dict) and isinstance(r.get("converted"), dict):
                        c = r["converted"]
                        lines.append(f"{labels.get(inst_id, inst_id)}: {r.get('value')} {r.get('unit')} "
                                     f"({c.get('value')} {c.get('unit')})")
                    elif isinstance(r, dict):
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


#: The words a person reads for each supplement reason (frozen by unit L2).
REASON_WORDS = {
    "correction": "Correction",
    "clarification": "Clarification",
    "addition": "Addition",
}


class SupplementPresenter:
    """A supplement as a person sees it: which signed entry it adds to and why, that entry's
    title line, then only what the supplement states (unit L2). The card is
    ``Supplement to #<seq> (<digest>), <Reason>`` and then ``  Original: TYPE · title``,
    ``  Title: …`` when given, one line per field and ``  Note: …`` for the body. It wraps the target type's
    presenter, so fields read with the same labels and units, and it adds no value: the original
    record it names is the one the statement points at."""

    def __init__(self, inner: Presenter, *, target: dict[str, Any], reason: str) -> None:
        self._inner = inner
        self._target = target
        self._reason = reason
        self.id = f"{inner.id}.supplement"
        self.version = inner.version

    def _head(self) -> str:
        t = self._target
        word = REASON_WORDS.get(self._reason, self._reason)
        return f"Supplement to #{t['seq']} ({str(t['digest'])[:8]}), {word}"

    def _original(self) -> str:
        return self._inner.render(self._target.get("content") or {}, "line")

    def render(self, content: dict[str, Any], density: str) -> str:
        if density not in DENSITIES:
            raise ValueError(f"unknown density {density!r}; one of {DENSITIES}")
        if density in ("strip", "line"):
            return self._head()
        # One indented ``Label: value`` line each, the shape a card is read in: the original,
        # the supplement's title, its fields, then its note. A note's later lines are indented
        # further, so they continue it rather than start a field.
        lines = [self._head(), f"  Original: {self._original()}"]
        if content.get("title"):
            lines.append(f"  Title: {content['title']}")
        # The wrapped card's first line is the type and title; the rest are the fields.
        lines += self._inner.render({**content, "title": ""}, "card").splitlines()[1:]
        if content.get("body"):
            first, *more = str(content["body"]).splitlines() or [""]
            lines += [f"  Note: {first}", *(f"    {x}" for x in more)]
        return "\n".join(lines)

    def speakable(self, content: dict[str, Any]) -> str:
        t = self._target
        head = f"Supplement to entry {t['seq']}, {REASON_WORDS.get(self._reason, self._reason)}"
        title = str((t.get("content") or {}).get("title", ""))
        rest = self._inner.speakable({**content, "title": ""})
        parts = [_say_numbers(html.escape(p, quote=False)) for p in (head, title) if p]
        return ". ".join(parts) + ". " + rest.lstrip(". ")

    def correctable_fields(self, content: dict[str, Any]) -> list[FieldRef]:
        return self._inner.correctable_fields(content)


class AckPresenter:
    """An acknowledgement as a person sees it: which signed entry they are acknowledging, then
    that entry's title and fields, read-only (unit L3). The card is ``Acknowledge #<seq>
    (<digest>)`` and then ``  Original: TYPE · title`` and one line per field of the target."""

    def __init__(self, inner: Presenter, *, target: dict[str, Any]) -> None:
        self._inner = inner
        self._target = target
        self.id = f"{inner.id}.ack"
        self.version = inner.version

    def _head(self) -> str:
        t = self._target
        return f"Acknowledge #{t['seq']} ({str(t['digest'])[:8]})"

    def render(self, content: dict[str, Any], density: str) -> str:
        if density not in DENSITIES:
            raise ValueError(f"unknown density {density!r}; one of {DENSITIES}")
        if density in ("strip", "line"):
            return self._head()
        target_content = self._target.get("content") or {}
        lines = [self._head(), f"  Original: {self._inner.render(target_content, 'line')}"]
        # The wrapped card's first line is the type and title; the rest are the fields.
        lines += self._inner.render({**target_content, "title": ""}, "card").splitlines()[1:]
        return "\n".join(lines)

    def speakable(self, content: dict[str, Any]) -> str:
        t = self._target
        title = str((t.get("content") or {}).get("title", ""))
        parts = [f"Acknowledge entry {t['seq']}", title]
        return ". ".join(_say_numbers(html.escape(p, quote=False)) for p in parts if p) + "."

    def correctable_fields(self, content: dict[str, Any]) -> list[FieldRef]:
        return []


class CosignPresenter(AckPresenter):
    """A co-signature as a person sees it: what they countersign, read-only (unit L4). The card
    is ``Co-sign #<seq> (<digest>)`` and then the entry as signed, line by line."""

    def __init__(self, inner: Presenter, *, target: dict[str, Any]) -> None:
        super().__init__(inner, target=target)
        self.id = f"{inner.id}.cosign"

    def _head(self) -> str:
        t = self._target
        return f"Co-sign #{t['seq']} ({str(t['digest'])[:8]})"

    def speakable(self, content: dict[str, Any]) -> str:
        t = self._target
        title = str((t.get("content") or {}).get("title", ""))
        parts = [f"Co-sign entry {t['seq']}", title]
        return ". ".join(_say_numbers(html.escape(p, quote=False)) for p in parts if p) + "."


class PlainPresenter:
    """An entry with no type, as an import may be (#1804): its title, then its text. Used only
    under a supplement to such an import."""

    def __init__(self, logbook_id: str, logbook_version: str) -> None:
        self.id = f"{logbook_id}.untyped"
        self.version = logbook_version

    def render(self, content: dict[str, Any], density: str) -> str:
        if density not in DENSITIES:
            raise ValueError(f"unknown density {density!r}; one of {DENSITIES}")
        title = str(content.get("title", ""))
        if density in ("strip", "line"):
            return title
        return title

    def speakable(self, content: dict[str, Any]) -> str:
        return _say_numbers(html.escape(str(content.get("title", "")), quote=False))

    def correctable_fields(self, content: dict[str, Any]) -> list[FieldRef]:
        return []


class SealPresenter:
    """A close-out as a person sees it (unit L5): the period covered, the chain head, every
    open item the server found, and the summary they wrote, each as an indented
    ``Label: value`` line so the web card reads the server's own text."""

    def __init__(self, logbook_id: str, logbook_version: str) -> None:
        self.id = f"{logbook_id}.seal"
        self.version = logbook_version

    @staticmethod
    def _lines(content: dict[str, Any]) -> list[str]:
        seal = content.get("seal") or {}
        covers = seal.get("covers") or {}
        items = seal.get("open_items") or []
        lines = [
            f"  Covers: #{covers.get('from_seq')} to #{covers.get('to_seq')}",
            f"  Chain head: #{covers.get('head_seq')} ({str(covers.get('head_digest', ''))[:8]})",
            f"  Open items: {len(items)}",
            *(f"  Open item: {i.get('summary') or i.get('kind')}" for i in items),
        ]
        first, *more = str(content.get("summary", "")).splitlines() or [""]
        return [*lines, f"  Summary: {first}", *(f"    {x}" for x in more)]

    def render(self, content: dict[str, Any], density: str) -> str:
        if density not in DENSITIES:
            raise ValueError(f"unknown density {density!r}; one of {DENSITIES}")
        title = str(content.get("title", ""))
        if density in ("strip", "line"):
            return title
        return "\n".join([title, *self._lines(content)])

    def speakable(self, content: dict[str, Any]) -> str:
        items = (content.get("seal") or {}).get("open_items") or []
        parts = [
            str(content.get("title", "")),
            f"{len(items)} open items" if items else "no open items",
            str(content.get("summary", "")),
        ]
        return ". ".join(_say_numbers(html.escape(p, quote=False)) for p in parts if p) + "."

    def correctable_fields(self, content: dict[str, Any]) -> list[FieldRef]:
        return [FieldRef("summary", "Summary", "text")]


_registered: dict[tuple[str, str], Presenter] = {}

#: A consumer's label for a record (#1802): ``fn(brief) -> {text, tone} | None``, per log.
_labels: dict[str, Any] = {}
#: Tones a label may take: information only, never success or warning (the page decides those).
LABEL_TONES = ("info",)
LABEL_MAX = 40
#: The base's own labels, by record kind: what a record is, said the same in every log.
KIND_LABELS = {
    "import": {"text": "Imported, not signed", "tone": "info"},
    # A whole import batch withdrawn (#1804): nobody signed the withdrawal either.
    "import_withdrawal": {"text": "Import withdrawn, not signed", "tone": "info"},
}


def register_label(logbook: str, label: Any) -> None:
    """A consumer's label for ``logbook``'s records, shown by the page without knowing what it
    means (#1802)."""
    _labels[logbook] = label


def unregister_label(logbook: str) -> None:
    _labels.pop(logbook, None)


def label_for(brief: dict[str, Any]) -> dict[str, str] | None:
    """The record's label: the base's for its kind, else the consumer's if well formed. A label
    function that fails, or answers anything but ``{text, tone: "info"}`` with a short text,
    gives no label: a label is information, never a claim."""
    base = KIND_LABELS.get(str(brief.get("kind") or ""))
    if base is not None:
        return dict(base)
    fn = _labels.get(str(brief.get("logbook") or ""))
    if fn is None:
        return None
    try:
        got = fn(dict(brief))
    except Exception:  # noqa: BLE001 - a consumer's label never breaks a read
        return None
    if not isinstance(got, dict) or set(got) != {"text", "tone"}:
        return None
    text, tone = got["text"], got["tone"]
    if not isinstance(text, str) or not text.strip() or len(text) > LABEL_MAX or tone not in LABEL_TONES:
        return None
    return {"text": text.strip(), "tone": tone}


def register(logbook: str, entry_type: str, presenter: Presenter) -> None:
    """Use ``presenter`` for one entry type instead of the default."""
    _registered[(logbook, entry_type)] = presenter


def unregister(logbook: str, entry_type: str) -> None:
    _registered.pop((logbook, entry_type), None)


def for_type(et: EntryType, *, logbook_version: str, site_id: str = "") -> Presenter:
    return _registered.get((et.logbook_id, et.id)) or LogbookPresenter(et, logbook_version, site_id)


__all__ = [
    "LogbookPresenter",
    "PlainPresenter",
    "DENSITIES",
    "FieldRef",
    "AckPresenter",
    "CosignPresenter",
    "Presenter",
    "REASON_WORDS",
    "SealPresenter",
    "SupplementPresenter",
    "for_type",
    "register",
    "unregister",
]
