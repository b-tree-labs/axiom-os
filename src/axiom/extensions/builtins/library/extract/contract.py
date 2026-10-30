# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The extractor contract (ADR-213): what a contributed extractor declares and returns.

An extractor looks at one stored file and says what is in it. It never writes
anywhere: it returns an :class:`ExtractionResult` and the pipeline decides where
each part goes, by the destination tags the result carries. That split is what
lets a domain pack or a data kit contribute extraction without being trusted to
touch bronze, the catalog, retrieval or a registry.

An extractor is two things:

* a **manifest** (:class:`ExtractorManifest`), declared in an extension's
  ``axiom-extension.toml`` as ``[[extractors]]`` or by an ``axiom.upload_extractors``
  entry point, saying which files it takes, what it produces, where that should
  go, how long it may take and how it is classified for export control;
* an **implementation**: any object with ``extract(ctx) -> ExtractionResult``, or
  a plain function of the same shape. It may also offer ``sniff(ctx) -> float``
  for a content match the manifest's rules cannot express.

The implementation reads the file through :class:`ExtractionContext`, which is
path-based so a multi-gigabyte file is never loaded whole. Raise
:class:`NotYet` for a format the extractor recognises but does not handle yet
(a stub), and :class:`ExtractorUnavailable` when an optional dependency is
missing; both are recorded as outcomes, not failures.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

#: What an extractor may produce (``outputs`` in its manifest).
OUTPUT_KINDS = ("metadata", "features", "text", "tables", "artifacts", "registrations", "preview")

#: Where outputs may be sent (``tags`` in its manifest). A tag is a request; the
#: pipeline's route for that tag decides, and an install without a route for a
#: tag holds the output and says so.
DESTINATION_TAGS = (
    "rag",  # extracted text, after the export-control hook, into the caller's corpus
    "dataset",  # tables to bronze-derived, then the bronze-to-silver conform hook
    "training-data",  # marked for a future training set (catalog only)
    "input-deck",  # a simulation input; a registry (a model registry) may take it
    "rom-artifact",  # a reduced-order-model artifact; a ROM registry may take it
    "reference-data",  # reference tables (catalog only, for consumers to read)
)

#: Export-control hook modes (``ec`` in a manifest).
#:   ``screen``  the extracted text goes through the marker screen (default);
#:   ``payload`` the file is an input to a simulation code: the controlled-code
#:               registry is consulted for the code the result names, then the screen;
#:   ``none``    nothing textual leaves the extractor (images, binaries).
EC_MODES = ("screen", "payload", "none")

#: How an extractor runs. ``thread`` shares the process with a timeout;
#: ``process`` runs in a spawned child with a memory limit and a hard kill.
ISOLATION = ("thread", "process")

#: Scope ranks, lowest first: a later scope composes after (and on a tie,
#: outranks) an earlier one. Mirrors ADR-208 D2.
SCOPE_RANK = {"core": 0, "pack": 1, "site": 2, "kit": 3}

HEAD_BYTES = 64 * 1024

_NAME_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+){2}$")


class ManifestError(ValueError):
    """An extractor manifest that would behave surprisingly if accepted."""


class NotYet(Exception):  # noqa: N818 - an outcome, not a failure
    """A format this extractor recognises but does not handle yet.

    The message is shown to the person; ``tracking`` names where the work is listed.
    """

    def __init__(self, message: str, *, tracking: str = "") -> None:
        super().__init__(message)
        self.tracking = tracking


class ExtractorUnavailable(Exception):  # noqa: N818 - an outcome, not a failure
    """A dependency this extractor needs is not installed here. Name the extra."""


@dataclass(frozen=True)
class MatchRule:
    """When an extractor applies. Any one signal matching is a match.

    Strength (used to order candidates of equal priority): magic > sniff > name
    > extension > mime.
    """

    extensions: tuple[str, ...] = ()
    #: Glob patterns on the file name (case-insensitive): ``outp``, ``mctal*``, ``*.i``.
    names: tuple[str, ...] = ()
    mime: tuple[str, ...] = ()
    #: Byte signatures, matched at ``magic_offset``.
    magic: tuple[bytes, ...] = ()
    magic_offset: int = 0
    #: Regular expressions over the decoded head of the file (first 64 KB).
    sniff: tuple[str, ...] = ()

    def strength(self, *, name: str, mime: str, head: bytes, head_text: str | None) -> int:
        lower = name.lower()
        best = 0
        if self.mime and mime and any(fnmatch.fnmatch(mime, m) for m in self.mime):
            best = max(best, 1)
        suffixes = [s.lower() for s in PurePosixPath(lower).suffixes]
        joined = "".join(suffixes[-2:]) if len(suffixes) >= 2 else ""
        if self.extensions and any(e in suffixes[-1:] or e == joined for e in self.extensions):
            best = max(best, 2)
        if self.names and any(fnmatch.fnmatch(lower, p.lower()) for p in self.names):
            best = max(best, 3)
        if self.sniff and head_text is not None and any(re.search(p, head_text, re.M) for p in self.sniff):
            best = max(best, 4)
        if self.magic:
            window = head[self.magic_offset:]
            if any(window.startswith(m) for m in self.magic):
                best = max(best, 5)
        return best


@dataclass(frozen=True)
class Limits:
    #: Larger files are stored and listed but not given to this extractor.
    max_bytes: int = 2 * 1024**3
    timeout_s: float = 60.0
    #: Enforced under ``process`` isolation (RLIMIT_AS); advisory under ``thread``.
    max_memory_mb: int = 1024


@dataclass(frozen=True)
class AcceptedType:
    """A file type an extractor adds to the library's allowlist."""

    extension: str
    mime: str = "application/octet-stream"
    #: ``text`` (must be UTF-8), ``binary`` (the extractor's magic decides) or a
    #: built-in signature name (``pdf``, ``png``, ``zip``, ``hdf5``...).
    sniff: str = "binary"
    inline: bool = False


@dataclass(frozen=True)
class ExtractorManifest:
    """What an extractor declares. Data; validated on registration."""

    #: ``<scope>.extract.<name>`` (ADR-208 D2 with the ``extract`` area).
    name: str
    version: str
    summary: str
    match: MatchRule
    outputs: tuple[str, ...]
    tags: tuple[str, ...] = ()
    priority: int = 0
    #: The scope that governs it: ``core``, ``pack``, ``site`` or ``kit``.
    scope: str = "core"
    #: The extension or kit that contributed it.
    contributed_by: str = ""
    entry: str = ""
    limits: Limits = Limits()
    ec: str = "screen"
    isolation: str = "thread"
    #: Extractors that do not run when this one matches the same file.
    supersedes: tuple[str, ...] = ()
    #: When this matches, no lower-priority extractor runs (except ``always`` ones).
    exclusive: bool = False
    #: Runs even when an exclusive extractor matched (cheap, universal facts).
    always: bool = False
    #: ``active`` or ``stub`` (recognises the format, says "not yet").
    status: str = "active"
    #: An ADR-208 reader this extractor's dataset output is shaped for, if any.
    reader: str = ""
    accepts: tuple[AcceptedType, ...] = ()
    #: Python modules the extractor needs; absent ones make it ``unavailable``.
    requires: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name) or self.name.split(".")[1] != "extract":
            raise ManifestError(f"extractor name {self.name!r} must be <scope>.extract.<name> in lower snake case")
        if not self.version or not re.match(r"^\d+\.\d+\.\d+", self.version):
            raise ManifestError(f"{self.name}: version {self.version!r} must be semantic (1.0.0)")
        bad = sorted(set(self.outputs) - set(OUTPUT_KINDS))
        if bad or not self.outputs:
            raise ManifestError(f"{self.name}: outputs {bad or '(none)'} not among {', '.join(OUTPUT_KINDS)}")
        bad = sorted(set(self.tags) - set(DESTINATION_TAGS))
        if bad:
            raise ManifestError(f"{self.name}: tags {bad} not among {', '.join(DESTINATION_TAGS)}")
        if self.ec not in EC_MODES:
            raise ManifestError(f"{self.name}: ec {self.ec!r} not among {', '.join(EC_MODES)}")
        if self.isolation not in ISOLATION:
            raise ManifestError(f"{self.name}: isolation {self.isolation!r} not among {', '.join(ISOLATION)}")
        if self.scope not in SCOPE_RANK:
            raise ManifestError(f"{self.name}: scope {self.scope!r} not among {', '.join(SCOPE_RANK)}")
        if self.status not in ("active", "stub"):
            raise ManifestError(f"{self.name}: status must be active or stub")
        if "text" in self.outputs and "rag" in self.tags and self.ec == "none":
            raise ManifestError(f"{self.name}: text bound for retrieval cannot skip the export-control hook")
        m = self.match
        if not (m.extensions or m.names or m.mime or m.magic or m.sniff):
            raise ManifestError(f"{self.name}: a match rule needs at least one signal")

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "scope": self.scope,
            "contributed_by": self.contributed_by,
            "status": self.status,
            "priority": self.priority,
            "outputs": list(self.outputs),
            "tags": list(self.tags),
            "ec": self.ec,
            "isolation": self.isolation,
            "match": {
                "extensions": list(self.match.extensions),
                "names": list(self.match.names),
                "mime": list(self.match.mime),
                "magic": [m.hex() for m in self.match.magic],
                "sniff": list(self.match.sniff),
            },
            "limits": {
                "max_bytes": self.limits.max_bytes,
                "timeout_s": self.limits.timeout_s,
                "max_memory_mb": self.limits.max_memory_mb,
            },
            "supersedes": list(self.supersedes),
            "exclusive": self.exclusive,
            "reader": self.reader,
        }


# ----------------------------------------------------------------------- inputs


@dataclass
class ExtractionContext:
    """One file, as an extractor sees it. Read it; never write through it."""

    path: Path
    file_name: str
    size: int
    sha256: str
    mime: str = "application/octet-stream"
    site: str = ""
    item_id: str = ""
    #: The member path when this file came out of an archive (``a.zip!/b/c.csv``).
    member: str = ""
    #: Archive nesting depth (0 for the uploaded file).
    depth: int = 0
    #: Results of extractors that already ran on this file, by name.
    prior: dict[str, ExtractionResult] = field(default_factory=dict)
    #: Other files that arrived with this one, by name: the archive's other members
    #: (a format that needs a companion file is uploaded with it in one archive). Read-only.
    siblings: dict[str, Path] = field(default_factory=dict)
    #: Values the person gave at upload (``extract_params``): a start time, a map name.
    params: dict[str, Any] = field(default_factory=dict)
    #: Called with a fraction 0..1 to report progress on a long extraction.
    progress: Callable[[float], None] | None = None
    _head: bytes | None = None

    @classmethod
    def for_path(cls, path: str | Path, *, file_name: str | None = None, **kw: Any) -> ExtractionContext:
        """A context for a file on disk (tests, kits trying an extractor locally)."""
        p = Path(path)
        h = hashlib.sha256()
        with p.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        return cls(path=p, file_name=file_name or p.name, size=p.stat().st_size, sha256=h.hexdigest(), **kw)

    @property
    def head(self) -> bytes:
        if self._head is None:
            with self.path.open("rb") as f:
                self._head = f.read(HEAD_BYTES)
        return self._head

    @property
    def head_text(self) -> str | None:
        """The head decoded as UTF-8 (a cut multi-byte character at the end is forgiven), or None."""
        data = self.head
        if b"\x00" in data[:8192]:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.start >= len(data) - 4:
                return data[: exc.start].decode("utf-8", errors="ignore")
            return None

    def sibling(self, pattern: str) -> Path | None:
        """The one sibling whose name matches ``pattern`` (a glob, case-insensitive), or None."""
        hits = [p for n, p in self.siblings.items() if fnmatch.fnmatch(n.lower(), pattern.lower())]
        return hits[0] if len(hits) == 1 else None

    def read_bytes(self, limit: int | None = None) -> bytes:
        with self.path.open("rb") as f:
            return f.read() if limit is None else f.read(limit)

    def read_text(self, limit: int | None = None, *, errors: str = "replace") -> str:
        return self.read_bytes(limit).decode("utf-8", errors=errors)

    def iter_lines(self, *, encoding: str = "utf-8") -> Iterable[str]:
        with self.path.open("r", encoding=encoding, errors="replace") as f:
            yield from f

    def report(self, fraction: float) -> None:
        if self.progress is not None:
            try:
                self.progress(max(0.0, min(1.0, float(fraction))))
            except Exception:  # noqa: BLE001 - progress is advisory
                pass


# ---------------------------------------------------------------------- outputs


@dataclass
class TextSection:
    """Text for retrieval. ``title`` becomes a heading in the indexed document."""

    text: str
    title: str = ""


@dataclass
class Table:
    """A table the file holds. ``rows`` is a bounded sample; ``row_count`` is the truth."""

    name: str
    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int | None = None
    #: Column name to a type name (``float``, ``int``, ``str``, ``datetime``...).
    types: dict[str, str] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    #: The ADR-208 reader (or schema ref) a conform step should read this with.
    schema_ref: str = ""
    #: Records shaped for silver (``feed``, ``channel``, ``ts``, ``value``, ``unit``...),
    #: when the extractor already produced them through a reader.
    records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Artifact:
    """A derived file: a preview image, a chart spec, a normalized copy."""

    name: str
    media_type: str
    content: bytes
    #: ``preview-chart``, ``thumbnail``, ``normalized``... free text.
    role: str = ""


@dataclass
class Registration:
    """A request for a registry to take this file: ``target`` names the route."""

    target: str  # "input-deck" | "rom-artifact" | another destination tag
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChildFile:
    """A member an archive extractor unpacked; the pipeline identifies and extracts it."""

    member: str
    path: Path


@dataclass
class ExtractionResult:
    """Everything one extractor found. All parts optional."""

    metadata: dict[str, Any] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)
    text: list[TextSection] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    registrations: list[Registration] = field(default_factory=list)
    children: list[ChildFile] = field(default_factory=list)
    #: A preview representation (see ``library.preview``), when this extractor
    #: knows how the file should be shown.
    preview: dict[str, Any] | None = None
    #: Destination tags beyond the manifest's.
    tags: list[str] = field(default_factory=list)
    #: Manifest tags this result does not want (a deck with no text drops ``rag``).
    drop_tags: list[str] = field(default_factory=list)
    #: For ``ec = "payload"``: the simulation code this file is an input to.
    code: str = ""
    warnings: list[str] = field(default_factory=list)
    #: One line saying what was found ("Widget log: 1,204 readings from line B7").
    summary: str = ""

    def to_json(self, *, sample_rows: int = 50) -> dict[str, Any]:
        """A JSON-able record of the result (artifact bytes and child paths omitted)."""
        return {
            "summary": self.summary,
            "metadata": _jsonable(self.metadata),
            "features": _jsonable(self.features),
            "text": [{"title": t.title, "chars": len(t.text)} for t in self.text],
            "tables": [
                {
                    "name": t.name,
                    "columns": list(t.columns),
                    "rows": _jsonable(t.rows[:sample_rows]),
                    "row_count": t.row_count if t.row_count is not None else len(t.rows),
                    "types": dict(t.types),
                    "units": dict(t.units),
                    "schema_ref": t.schema_ref,
                    "records": len(t.records),
                }
                for t in self.tables
            ],
            "artifacts": [
                {"name": a.name, "media_type": a.media_type, "role": a.role, "size": len(a.content)}
                for a in self.artifacts
            ],
            "registrations": [{"target": r.target, "payload": _jsonable(r.payload)} for r in self.registrations],
            "children": [c.member for c in self.children],
            "preview": _jsonable(self.preview) if self.preview is not None else None,
            "tags": list(self.tags),
            "drop_tags": list(self.drop_tags),
            "code": self.code,
            "warnings": list(self.warnings),
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, bytes):
        return value.hex()[:64]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    return str(value)


@runtime_checkable
class Extractor(Protocol):
    def extract(self, ctx: ExtractionContext) -> ExtractionResult: ...


def jsonable(value: Any) -> Any:
    """Make a value JSON-safe the way results are stored (numpy, datetimes, bytes, NaN)."""
    return _jsonable(value)


# ------------------------------------------------------------------- manifest IO


def _strs(v: Any) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    return tuple(str(x) for x in v)


def manifest_from_dict(data: dict[str, Any], *, scope: str = "", contributed_by: str = "") -> ExtractorManifest:
    """Build a manifest from a ``[[extractors]]`` table (TOML) or an equivalent dict."""
    match = data.get("match") or {}
    magic: list[bytes] = []
    for m in _strs(match.get("magic")):
        magic.append(bytes.fromhex(m[4:]) if m.startswith("hex:") else m.encode("latin-1"))
    lim = data.get("limits") or {}
    accepts = tuple(
        AcceptedType(
            extension=str(a["extension"]).lower(),
            mime=str(a.get("mime", "application/octet-stream")),
            sniff=str(a.get("sniff", "binary")),
            inline=bool(a.get("inline", False)),
        )
        for a in (data.get("accepts") or [])
    )
    return ExtractorManifest(
        name=str(data.get("name", "")),
        version=str(data.get("version", "")),
        summary=str(data.get("summary", "")),
        match=MatchRule(
            extensions=tuple(e.lower() for e in _strs(match.get("extensions"))),
            names=_strs(match.get("names")),
            mime=_strs(match.get("mime")),
            magic=tuple(magic),
            magic_offset=int(match.get("magic_offset", 0)),
            sniff=_strs(match.get("sniff")),
        ),
        outputs=_strs(data.get("outputs")),
        tags=_strs(data.get("tags")),
        priority=int(data.get("priority", 0)),
        scope=scope or str(data.get("scope", "core")),
        contributed_by=contributed_by or str(data.get("contributed_by", "")),
        entry=str(data.get("entry", "")),
        limits=Limits(
            max_bytes=int(lim.get("max_bytes", Limits.max_bytes)),
            timeout_s=float(lim.get("timeout_s", Limits.timeout_s)),
            max_memory_mb=int(lim.get("max_memory_mb", Limits.max_memory_mb)),
        ),
        ec=str(data.get("ec", "screen")),
        isolation=str(data.get("isolation", "thread")),
        supersedes=_strs(data.get("supersedes")),
        exclusive=bool(data.get("exclusive", False)),
        always=bool(data.get("always", False)),
        status=str(data.get("status", "active")),
        reader=str(data.get("reader", "")),
        accepts=accepts,
        requires=_strs(data.get("requires")),
    )


__all__ = [
    "DESTINATION_TAGS",
    "EC_MODES",
    "HEAD_BYTES",
    "OUTPUT_KINDS",
    "SCOPE_RANK",
    "AcceptedType",
    "Artifact",
    "ChildFile",
    "ExtractionContext",
    "ExtractionResult",
    "Extractor",
    "ExtractorManifest",
    "ExtractorUnavailable",
    "Limits",
    "ManifestError",
    "MatchRule",
    "NotYet",
    "Registration",
    "Table",
    "TextSection",
    "jsonable",
    "manifest_from_dict",
]
