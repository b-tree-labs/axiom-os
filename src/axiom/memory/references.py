# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reference fragments: memory that points at an organizational record (ADR-251 D2; unit LD-3).

A **reference fragment** is a ``resource`` fragment (ADR-088: metadata plus pointer, never blob
content) whose content is exactly:

- ``ref``: the record's pointer, ``axiom://<scheme>/sha256:<digest>``;
- ``reference``: the pointer's parts, ``{scheme, id, scope, seq, site, digest}``;
- ``summary``: a short summary in the writer's own words;
- ``labels``: the record's labels at write time (``classification``, ``export_controlled``).

Provenance is the fragment's ``(T, U, A, R)`` tuple, with the pointer as its resource, and a
:class:`~axiom.memory.fragment.SourceOrigin` naming the record's home and the pointer. The
fragment holds **none of the record's content**: writing one that copies a content value is
refused, and so is one with no labels or one made from anything that is not a signed record.

**Reading back** never trusts the fragment. :func:`resolve` re-resolves the pointer through the
record home's door with the caller's rights *at that moment*: a denied pointer hides the summary
(the fragment is not served), an allowed one returns the record as of the home's watermark with a
deep link, and the door writes its one read record. A record whose content was removed under its
retention rule resolves to its tombstone, never to anything remembered.

A record home supplies a :class:`Resolver` for its pointer scheme (:func:`register`). This module
names no home: it knows the shape, the refusals and the order, and asks the home the rest.
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

#: The content keys of a reference fragment, and nothing else.
CONTENT_KEYS = frozenset({"ref", "reference", "summary", "labels"})
#: The parts of a pointer.
REFERENCE_KEYS = frozenset({"scheme", "id", "scope", "seq", "site", "digest"})
#: The labels a reference carries (the doors' vocabulary, ADR-251 D8).
LABEL_KEYS = ("classification", "export_controlled")
#: A summary is short: a line a person reads, not a copy.
SUMMARY_MAX = 280

_SCHEME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
log = logging.getLogger("axiom.memory.references")
_POINTER = re.compile(r"^axiom://([a-z][a-z0-9_]*)/sha256:([0-9a-f]{64})$")


class ReferenceRefused(ValueError):
    """A reference that may not be written. ``code`` is the door's answer when a door refused."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# -- the record home's side -------------------------------------------------------------------


@dataclass(frozen=True)
class RecordFacts:
    """What a home says about one signed record, for writing a reference to it. ``content`` is
    read only to refuse a summary that copies it; it is never stored."""

    scheme: str
    id: str
    scope: str
    seq: int
    site: str | None
    digest: str
    labels: Mapping[str, Any] | None
    content: Any = None

    @property
    def pointer(self) -> str:
        return pointer(self.scheme, self.digest)


@dataclass(frozen=True)
class Resolution:
    """One re-resolution of a pointer through its home's door.

    ``code`` is the door's answer on a refusal (the same code every transport gives); the exact
    reason is in the home's read record only. ``record`` is the record as of ``as_of`` through the
    door; a tombstoned record has ``record`` None and ``tombstone`` set."""

    allowed: bool
    code: str | None = None
    record: dict[str, Any] | None = None
    as_of: str | None = None
    link: str | None = None
    url: str | None = None
    labels: Mapping[str, Any] | None = None
    tombstone: dict[str, Any] | None = None

    @property
    def tombstoned(self) -> bool:
        return self.tombstone is not None

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "code": self.code, "record": self.record,
                "as_of": self.as_of, "link": self.link, "url": self.url,
                "labels": dict(self.labels) if self.labels else None,
                "tombstoned": self.tombstoned, "tombstone": self.tombstone}  # fmt: skip


class Resolver(Protocol):
    """What a record home provides for its pointer scheme."""

    def facts(self, record: str) -> RecordFacts:
        """The signed record named by id or pointer; raises :class:`ReferenceRefused` for a
        draft, a tray item or anything else that is not a signed record."""

    def admit(self, caller: Any, facts: RecordFacts) -> str | None:
        """The door's code when ``caller`` may not read the record now, else None. Writes the
        door's read record."""

    def caller_for(self, principal: str) -> Any:
        """The caller a principal reads as, when a transport names only the principal."""

    def resolve(self, reference: Mapping[str, Any], *, caller: Any, transport: str) -> Resolution:
        """Re-resolve through the door with the caller's rights now; writes one read record."""

    def current_labels(self, reference: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The record's labels now (metadata only, no read of content), or None if unknown."""


_resolvers: dict[str, Resolver] = {}
_lock = threading.Lock()


def register(scheme: str, resolver: Resolver | None) -> None:
    if not _SCHEME.match(scheme):
        raise ValueError(f"{scheme!r} is not a pointer scheme")
    with _lock:
        if resolver is None:
            _resolvers.pop(scheme, None)
        else:
            _resolvers[scheme] = resolver


def resolver_for(scheme: str) -> Resolver | None:
    """The home's resolver. A home that has not loaded yet is loaded by its name
    (``axiom.extensions.builtins.<scheme>.references``); an unknown scheme has none."""
    if not isinstance(scheme, str) or not _SCHEME.match(scheme):
        return None
    if scheme not in _resolvers:
        try:
            importlib.import_module(f"axiom.extensions.builtins.{scheme}.references")
        except ImportError:
            return None
    return _resolvers.get(scheme)


def pointer(scheme: str, digest: str) -> str:
    return f"axiom://{scheme}/sha256:{digest}"


# -- retirement: a record's content removed under its retention rule (LD-6) ----------------------

#: Who removes fragments pointing at a record whose content went: each memory store that holds
#: reference fragments registers ``fn(pointers, tombstone) -> int`` (fragments retired).
_retirers: dict[str, Any] = {}


def register_retirer(name: str, fn: Any) -> None:
    """Register (or with None, remove) a store's retirer of reference fragments."""
    with _lock:
        if fn is None:
            _retirers.pop(name, None)
        else:
            _retirers[name] = fn


def retire(pointers: Iterable[str], *, tombstone: Mapping[str, Any]) -> int:
    """Tell every registered store that the records ``pointers`` name lost their content under a
    tombstone, so their reference fragments leave recall (ADR-251 D2, unit LD-6). Recall of a
    fragment a store still holds resolves to the tombstone either way. Returns how many stores
    answered; a store that fails is logged and the others still run."""
    wanted = [p for p in pointers if _POINTER.match(str(p))]
    if not wanted:
        return 0
    try:
        # The node's own store registers itself on import (axiom.extensions.builtins.memory.retire).
        importlib.import_module("axiom.extensions.builtins.memory.retire")
    except ImportError:
        pass
    with _lock:
        fns = list(_retirers.items())
    done = 0
    for name, fn in fns:
        try:
            fn(list(wanted), dict(tombstone))
            done += 1
        except Exception:  # noqa: BLE001 - one store's failure never stops another's
            log.warning("memory store %s did not retire references for tombstone %s", name,
                        tombstone.get("attestation_id"), exc_info=True)  # fmt: skip
    return done


# -- shape --------------------------------------------------------------------------------------


def is_reference(content: Mapping[str, Any] | None) -> bool:
    """True for a reference fragment's content (a resource carrying ``reference``)."""
    return isinstance(content, Mapping) and "reference" in content


def labels_valid(labels: Any) -> bool:
    from axiom.governance.classification import parse_label

    return (isinstance(labels, Mapping) and set(labels) == set(LABEL_KEYS)
            and parse_label(str(labels.get("classification") or "")) is not None
            and isinstance(labels.get("export_controlled"), bool))  # fmt: skip


def check_shape(content: Mapping[str, Any]) -> None:
    """The shape every reference fragment has, enforced on the one write path. Raises
    :class:`ValueError` (a :class:`ReferenceRefused`)."""
    extra = set(content) - CONTENT_KEYS
    if extra:
        raise ReferenceRefused(f"a reference holds only {sorted(CONTENT_KEYS)}; fields {sorted(extra)} "
                               "are not allowed (a reference never copies its record)")  # fmt: skip
    if not labels_valid(content.get("labels")):
        raise ReferenceRefused("a reference carries its record's labels; this one has no labels")
    ref = content.get("reference")
    if not isinstance(ref, Mapping) or set(ref) != REFERENCE_KEYS:
        raise ReferenceRefused(f"a reference names its record by {sorted(REFERENCE_KEYS)}")
    if content.get("ref") != pointer(str(ref.get("scheme")), str(ref.get("digest"))) or not _POINTER.match(
        str(content.get("ref"))
    ):
        raise ReferenceRefused("a reference's pointer is axiom://<home>/sha256:<digest> of its record")
    summary = content.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > SUMMARY_MAX:
        raise ReferenceRefused(f"a reference's summary is one short line (1 to {SUMMARY_MAX} characters)")


def _leaves(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return [x for v in value.values() for x in _leaves(v)]
    if isinstance(value, (list, tuple)):
        return [x for v in value for x in _leaves(v)]
    return [value]


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def copied_value(summary: str, content: Any) -> str | None:
    """A value of the record's content that the summary repeats, or None.

    A phrase (two words or more, or eight characters or more) is refused anywhere in the summary;
    a number is refused as a whole token. A single short word is not a copy (``ok``, ``seal``)."""
    text = _norm(summary)
    for leaf in _leaves(content):
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, (int, float)):
            leaf = str(leaf)
        if not isinstance(leaf, str) or not leaf.strip():
            continue
        v = _norm(leaf)
        if re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", v):
            if re.search(rf"(?<![\w.]){re.escape(v)}(?![\w])", text):
                return leaf
        elif (len(v.split()) >= 2 or len(v) >= 8) and v in text:
            return leaf
    return None


def vet(content: Mapping[str, Any]) -> None:
    """The one write path's check for a reference fragment: its shape, and that its record is a
    signed record the home knows, with these labels, and that the summary copies none of it."""
    from axiom.memory.serving import looks_like_secret

    check_shape(content)
    ref = content["reference"]
    resolver = resolver_for(str(ref["scheme"]))
    if resolver is None:
        raise ReferenceRefused(f"no home resolves {ref['scheme']!r} pointers on this node")
    facts = resolver.facts(str(ref["id"]))
    if (facts.digest, facts.scope, facts.seq, facts.site) != (ref["digest"], ref["scope"], ref["seq"],
                                                              ref["site"]):  # fmt: skip
        raise ReferenceRefused("the pointer does not name this signed record")
    if not labels_valid(facts.labels) or dict(facts.labels or {}) != dict(content["labels"]):
        raise ReferenceRefused("a reference carries its record's labels at write time")
    if copied_value(content["summary"], facts.content) is not None:
        raise ReferenceRefused("the summary repeats the record's content; a reference never copies it")
    if looks_like_secret(content["summary"]):
        raise ReferenceRefused("the summary reads as a secret")


# -- writing ------------------------------------------------------------------------------------


def write_reference(
    composition: Any,
    *,
    record: str,
    summary: str,
    principal_id: str,
    agents: set[str],
    caller: Any = None,
    scheme: str = "attest",
    accountable_human_id: Any = None,
    session_id: str | None = None,
) -> Any:
    """Write a reference fragment through ``CompositionService.write`` (the one write path).

    ``record`` is a record id or a pointer. The writer must be able to read the record now
    (the home's door decides, with ``caller`` or the principal's own rights); the labels are
    copied from the record; nothing of the record's content is stored."""
    from axiom.memory.fragment import SourceOrigin

    m = _POINTER.match(str(record))
    if m:
        scheme = m.group(1)
    resolver = resolver_for(scheme)
    if resolver is None:
        raise ReferenceRefused(f"no home resolves {scheme!r} pointers on this node")
    facts = resolver.facts(str(record))
    if not labels_valid(facts.labels):
        raise ReferenceRefused("the record's log declares no labels; an unlabeled reference is refused")
    code = resolver.admit(caller if caller is not None else resolver.caller_for(principal_id), facts)
    if code is not None:
        raise ReferenceRefused("the writer may not read this record", code=code)
    content = {
        "ref": facts.pointer,
        "reference": {"scheme": facts.scheme, "id": facts.id, "scope": facts.scope,
                      "seq": facts.seq, "site": facts.site, "digest": facts.digest},
        "summary": " ".join(str(summary or "").split()),
        "labels": {k: facts.labels[k] for k in LABEL_KEYS},  # type: ignore[index]
    }  # fmt: skip
    origin = SourceOrigin(harness=f"axiom:{facts.scheme}", account=principal_id,
                          source_ref=facts.pointer, imported_at=datetime.now(UTC).isoformat())  # fmt: skip
    kwargs: dict[str, Any] = {"origin": origin, "session_id": session_id}
    if accountable_human_id is not None:
        kwargs["accountable_human_id"] = accountable_human_id
    return composition.write(content=content, cognitive_type="resource", principal_id=principal_id,
                             agents=set(agents), resources={facts.pointer}, **kwargs)  # fmt: skip


# -- reading back -------------------------------------------------------------------------------

#: The code a refusal carries when nothing could decide (no home, an error).
UNDECIDED = "policy_unavailable"


def resolve(content: Mapping[str, Any], *, principal: str, caller: Any = None,
            transport: str = "mcp") -> Resolution:  # fmt: skip
    """Re-resolve a reference through its home's door, with the caller's rights now. Never
    raises: anything that cannot be decided is a refusal (fail closed)."""
    try:
        check_shape(content)
        ref = content["reference"]
        resolver = resolver_for(str(ref["scheme"]))
        if resolver is None:
            return Resolution(False, UNDECIDED)
        who = caller if caller is not None else resolver.caller_for(principal)
        return resolver.resolve(ref, caller=who, transport=transport)
    except Exception:  # noqa: BLE001 - a reference that cannot be resolved is not served
        return Resolution(False, UNDECIDED)


def current_labels(content: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The labels a reference must be judged by now: the stricter of the copy and the record's
    current labels (a relabel upward after the write is honoured; downward is not trusted
    without a read through the door)."""
    from axiom.governance.classification import parse_label

    stamped = content.get("labels")
    try:
        resolver = resolver_for(str(content["reference"]["scheme"]))
        now = resolver.current_labels(content["reference"]) if resolver is not None else None
    except Exception:  # noqa: BLE001
        now = None
    if not labels_valid(stamped):
        return None
    if not labels_valid(now):
        return dict(stamped)
    a, b = parse_label(stamped["classification"]), parse_label(now["classification"])  # type: ignore[index]
    worse = a if a.tier >= b.tier else b  # type: ignore[union-attr]
    return {"classification": worse.value,
            "export_controlled": bool(stamped["export_controlled"] or now["export_controlled"])}  # type: ignore[index]  # fmt: skip


def render(content: Mapping[str, Any]) -> str:
    """The text a reference is recalled and synced as: its pointer and its summary, only."""
    return f"ref: {content.get('ref', '')}\nsummary: {content.get('summary', '')}"


def render_resolved(content: Mapping[str, Any], resolution: Resolution) -> str:
    """The text served for an admitted reference: pointer and summary, when the record stands,
    and as of when; a tombstoned record is served as its tombstone, with nothing remembered."""
    if resolution.tombstoned:
        return (f"ref: {content.get('ref', '')}\n"
                "tombstone: the record's content was removed under its retention rule")  # fmt: skip
    return f"{render(content)}\nas_of: {resolution.as_of or ''}\nlink: {resolution.link or ''}"


def listed_summary(content: Mapping[str, Any] | None) -> str:
    """The summary an ungated listing may show: none for a reference (its summary is served only
    after its pointer is re-resolved through the record's door), the fragment's own otherwise."""
    if not isinstance(content, Mapping) or is_reference(content):
        return ""
    return str(content.get("summary") or "")


def listed_content(content: Mapping[str, Any] | None) -> Any:
    """The content an ungated listing may show: a reference's pointer only."""
    return withheld(content) if is_reference(content) else content  # type: ignore[arg-type]


def withheld(content: Mapping[str, Any]) -> dict[str, Any]:
    """What a surface that does not resolve references may show of one: the pointer only."""
    return {"ref": content.get("ref", ""), "summary": "", "note": "a reference: recall resolves it"}


__all__ = [
    "register_retirer",
    "retire",
    "CONTENT_KEYS",
    "LABEL_KEYS",
    "REFERENCE_KEYS",
    "SUMMARY_MAX",
    "RecordFacts",
    "ReferenceRefused",
    "Resolution",
    "Resolver",
    "check_shape",
    "copied_value",
    "current_labels",
    "is_reference",
    "labels_valid",
    "listed_content",
    "listed_summary",
    "pointer",
    "register",
    "render",
    "render_resolved",
    "resolve",
    "resolver_for",
    "vet",
    "withheld",
    "write_reference",
]
