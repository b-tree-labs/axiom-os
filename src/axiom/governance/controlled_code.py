# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Controlled-code registry: what is controlled, on what basis, under what rule.

**"Code" here means a named simulation program**, in the sense the modelling and
simulation community uses the word: a specific, named, distributable program
that a site holds under an export-control determination. It does **not** mean
source code in general, and this registry says nothing about source files.

## What this is not

**This module declares. It does not enforce.** Nothing here gates, permits,
denies or filters. Enforcement is settled and lives elsewhere: a local run
self-gates on filesystem permissions, and a managed run goes through a job
broker that checks the authorized-persons list. The registry exists so that
tooling can *ask* whether an artifact is controlled and avoid mishandling it.
If you find yourself wanting ``registry.permit(...)``, you are in the wrong
module.

## The doctrine, stated once so it is not re-derived wrongly

Export control attaches to the **controlled codes themselves**. A controlled
code cannot leave the enclave in which cleared people interact with it. Its
**inputs, outputs and results may be interacted with, and may enter and leave
the enclave freely**.

Reactor and research data, inputs, outputs and results are **not**
export-controlled. Such data may still be access-controlled for proprietary or
personal-information reasons, which is a separate, lighter regime that does not
turn on nationality and is not modelled here. There is deliberately no way to
express "controlled data" in this schema, and the loader refuses the attempt
with an explanation.

## Three outcomes, never two

:meth:`ControlledCodeRegistry.lookup` returns a :class:`ControlFinding` whose
``standing`` is one of ``CONTROLLED``, ``NOT_CONTROLLED`` or ``UNKNOWN``. The
list of declarations is incomplete by construction and will stay incomplete for
some time, so ``UNKNOWN`` is the *ordinary* answer, not an edge case.

``UNKNOWN`` is not ``NOT_CONTROLLED``. A predicate that answered ``False`` for
an artifact nobody had declared would read at every call site as "this is fine",
and a control that appears to exist but permits everything is worse than no
control because it stops people looking. So a finding refuses to be truth-tested
at all: ``bool(finding)`` raises, in every standing, and the caller has to branch
on ``standing`` or call one of the named accessors. The one boolean on offer,
:attr:`ControlFinding.known_not_controlled`, is safe in both directions because
its ``False`` side covers both "controlled" and "nobody has said".

## Sources and the conflict policy

Sites differ and the declared set will change, so a registry composes any number
of declaration sources: manifest files via :func:`load_manifest`, or in-process
entries via :meth:`Declarations.from_entries`.

Two sources may declare the same artifact only if they declare it **identically**
(a vendored or duplicated manifest must still boot). Any difference is refused
with :class:`ConflictingDeclaration` naming the artifact, both sources and the
fields that differ. Composition never picks a winner, and refusing is symmetric,
so the outcome does not depend on the order the sources were listed in.

## Declaring an entry

Axiom ships this mechanism and no entries. A domain declares its own controlled
codes in its own repository, in a manifest like this one (the artifact name below
is fictional)::

    # controlled-codes.toml
    schema_version = 1

    [[code]]
    artifact = "code://fluxomatic"
    status = "controlled"
    classification = "controlled"

    [code.basis]
    regime = "10 CFR Part 810"
    citation = "site determination 2026-014"
    determined_by = "@export-control-officer:example-org"
    determined_on = 2026-01-15

    [code.access_rule]
    enclave = "example-enclave"
    authorized_persons_ref = "roster://example-enclave/authorized-persons"

Then::

    registry = ControlledCodeRegistry.from_manifests("controlled-codes.toml")
    finding = registry.lookup("fluxomatic")
    if finding.standing is ControlStanding.CONTROLLED:
        ...

An artifact the site has examined and found *not* controlled is worth declaring
too, with ``status = "not_controlled"`` and a basis but no regime, no access rule
and no classification. That is how a site converts an ``UNKNOWN`` into a
positive answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, NoReturn

from axiom.governance.classification import Classification
from axiom.governance.resource import ResourceRef

# The stdlib parser, taken from the house import point. Deliberately not
# `toml_compat.load_toml`, which returns an empty dict on any failure: a
# manifest that fails to parse must not be indistinguishable from a site that
# has declared nothing.
from axiom.infra.toml_compat import tomllib

#: The one scheme a declared artifact may carry. Confining the registry to
#: ``code://`` is what makes "export-controlled data" unrepresentable here.
CODE_SCHEME = "code"

#: Manifest schema version this build understands. A manifest must say so.
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Errors. All ValueError, matching the house habit for loader failures, and all
# specific enough to catch on their own.
# ---------------------------------------------------------------------------


class ControlledCodeError(ValueError):
    """Base for every controlled-code registry failure."""


class ManifestError(ControlledCodeError):
    """A declaration source is missing, unreadable or malformed.

    The message always begins with the source that caused it, so an operator
    reading a stack trace knows which file to open.
    """


class ConflictingDeclaration(ControlledCodeError):
    """Two sources declare the same artifact differently.

    Never resolved by precedence. Composition refuses, and names both sources.
    """


class UndeclaredArtifact(ControlledCodeError):
    """Raised by :meth:`ControlFinding.require_declared` on an ``UNKNOWN``."""


# ---------------------------------------------------------------------------
# Vocabulary.
# ---------------------------------------------------------------------------


class ControlStatus(str, Enum):
    """What a declaration *says* about an artifact. Two values, because a
    declaration is always an assertion. Absence of a declaration is not a value
    here; see :class:`ControlStanding`.
    """

    CONTROLLED = "controlled"
    NOT_CONTROLLED = "not_controlled"


class ControlStanding(str, Enum):
    """What a *query* returns. Three values, and the third is load-bearing.

    ``UNKNOWN`` means nobody has declared this artifact either way. It is not a
    synonym for ``NOT_CONTROLLED`` and must never be treated as one.
    """

    CONTROLLED = "controlled"
    NOT_CONTROLLED = "not_controlled"
    UNKNOWN = "unknown"


def code_ref(name: str) -> ResourceRef:
    """Build the :class:`ResourceRef` for a named simulation code.

    ``code_ref("fluxomatic")`` is ``code://fluxomatic``.
    """
    cleaned = name.strip()
    if not cleaned:
        raise ControlledCodeError("a code name cannot be empty")
    return ResourceRef(scheme=CODE_SCHEME, identifier=cleaned)


def _as_artifact(artifact: ResourceRef | str) -> ResourceRef:
    """Coerce a query argument. A bare name means a code; a URI is parsed."""
    if isinstance(artifact, ResourceRef):
        return artifact
    return ResourceRef.parse(artifact) if "://" in artifact else code_ref(artifact)


# ---------------------------------------------------------------------------
# The entry: identity, control basis, access rule.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlBasis:
    """Why the determination says what it says, and who made it.

    ``regime`` is free text (``"10 CFR Part 810"``, an EAR category, a site
    instrument) rather than a closed enum: refusing a site's own name for the
    authority under which it made a determination would block the declaration
    of something that really is controlled, which is the wrong way to fail.
    It is required on a ``CONTROLLED`` entry and forbidden on a
    ``NOT_CONTROLLED`` one, where there is no regime doing any controlling.
    """

    citation: str
    """The determination this entry records, e.g. a control-office reference."""

    determined_by: str
    """The authority that made it, as a principal handle."""

    regime: str | None = None
    determined_on: date | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.citation or not self.citation.strip():
            raise ControlledCodeError(
                "ControlBasis.citation cannot be empty: an entry with no "
                "determination behind it is a rumour, not a basis"
            )
        if not self.determined_by or not self.determined_by.strip():
            raise ControlledCodeError(
                "ControlBasis.determined_by cannot be empty: a determination "
                "nobody is named as making cannot be re-examined later"
            )
        if self.regime is not None and not self.regime.strip():
            raise ControlledCodeError("ControlBasis.regime cannot be blank; omit it instead")


@dataclass(frozen=True)
class AccessRule:
    """The rule governing authorized interaction with a controlled code.

    The rule is the same in every case the registry can express, which is why
    the two egress facts are read-only properties and not fields: the code stays
    in its enclave, and its inputs and outputs do not.
    """

    enclave: str
    """The boundary the code may not leave, named as the site names it."""

    authorized_persons_ref: str | None = None
    """Pointer to the list the job broker checks. The registry never holds the
    names themselves; personal information does not belong in a manifest."""

    note: str | None = None

    def __post_init__(self) -> None:
        if not self.enclave or not self.enclave.strip():
            raise ControlledCodeError(
                "AccessRule.enclave cannot be empty: a rule that names no boundary states nothing"
            )

    @property
    def artifact_may_egress(self) -> bool:
        """Always ``False``. The controlled code stays inside ``enclave``."""
        return False

    @property
    def inputs_and_outputs_may_egress(self) -> bool:
        """Always ``True``. Inputs, outputs and results are not export-controlled.

        Deliberately not a field. Data may still be access-controlled for
        proprietary or personal-information reasons, but that is a different
        regime and is not declared here.
        """
        return True


@dataclass(frozen=True)
class ControlledCodeEntry:
    """One declaration: an artifact, what it is, why, and the rule if controlled.

    Identity is the exact :class:`ResourceRef`. Lookup is exact, never prefix or
    fuzzy, so a near-miss name reports ``UNKNOWN`` rather than borrowing another
    entry's answer. Versions, if a site needs them, live inside the identifier.

    Invariants, enforced in both directions so an entry cannot be half-stated:
    a ``CONTROLLED`` entry carries a regime, an access rule and a classification
    of ``REGULATED`` or ``CONTROLLED``; a ``NOT_CONTROLLED`` entry carries none
    of those and still carries a basis.
    """

    artifact: ResourceRef
    status: ControlStatus
    basis: ControlBasis
    access_rule: AccessRule | None = None
    classification: Classification | None = None

    def __post_init__(self) -> None:
        if self.artifact.scheme != CODE_SCHEME:
            raise ControlledCodeError(
                f"{self.artifact} cannot be declared here: this registry covers "
                f"{CODE_SCHEME}:// artifacts, meaning named simulation programs. "
                "Export control attaches to the code itself; inputs, outputs and "
                "results are not export-controlled"
            )
        if not self.artifact.identifier.strip():
            raise ControlledCodeError("a declared artifact must name a code")

        if self.status is ControlStatus.CONTROLLED:
            self._check_controlled()
        else:
            self._check_not_controlled()

    def _check_controlled(self) -> None:
        if self.basis.regime is None:
            raise ControlledCodeError(
                f"{self.artifact} is declared controlled but names no regime; "
                "the basis must say under what authority"
            )
        if self.access_rule is None:
            raise ControlledCodeError(
                f"{self.artifact} is declared controlled but carries no access "
                "rule; a control with no stated rule cannot be honoured"
            )
        if self.classification is None:
            raise ControlledCodeError(
                f"{self.artifact} is declared controlled but carries no "
                "classification; state the tier the site assigns it"
            )
        if self.classification.tier < Classification.REGULATED.tier:
            raise ControlledCodeError(
                f"{self.artifact} is declared controlled at "
                f"{self.classification.value!r}, which is a contradiction; "
                f"expected {Classification.REGULATED.value!r} or "
                f"{Classification.CONTROLLED.value!r}"
            )

    def _check_not_controlled(self) -> None:
        if self.basis.regime is not None:
            raise ControlledCodeError(
                f"{self.artifact} is declared not controlled but names the "
                f"regime {self.basis.regime!r}; nothing is controlling it"
            )
        if self.access_rule is not None:
            raise ControlledCodeError(
                f"{self.artifact} is declared not controlled but carries an "
                "access rule; there is no enclave to keep it inside"
            )
        if self.classification is not None:
            raise ControlledCodeError(
                f"{self.artifact} is declared not controlled but carries a "
                "classification; this registry covers export control only, and "
                "a proprietary or personal-information tier is set elsewhere"
            )


# ---------------------------------------------------------------------------
# The query result. Three outcomes, and no way to squint at it as a boolean.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlFinding:
    """The answer to one lookup: a standing, and the declaration behind it.

    Truth-testing raises. ``if registry.lookup(x):`` is the exact shape of the
    bug this type exists to prevent, so it fails immediately and loudly rather
    than reading as "controlled" or as "safe" depending on who wrote it.
    """

    artifact: ResourceRef
    standing: ControlStanding
    entry: ControlledCodeEntry | None = None
    source: str | None = None
    """Which declaration source answered. ``None`` when nothing did."""

    def __bool__(self) -> NoReturn:
        raise TypeError(
            f"a ControlFinding for {self.artifact} is not a boolean: its "
            f"standing is {self.standing.value!r}, and an undeclared artifact "
            "is UNKNOWN rather than safe. Branch on `.standing`, or use "
            "`.known_not_controlled` / `.require_declared()`"
        )

    @property
    def is_declared(self) -> bool:
        """Whether any source has said anything about this artifact."""
        return self.standing is not ControlStanding.UNKNOWN

    @property
    def known_not_controlled(self) -> bool:
        """``True`` only on an explicit not-controlled declaration.

        Safe in both directions: ``False`` covers both a controlled artifact and
        one nobody has declared, so a caller that treats ``False`` as "handle
        carefully" is right in both cases.
        """
        return self.standing is ControlStanding.NOT_CONTROLLED

    def require_declared(self) -> ControlledCodeEntry:
        """Return the entry, or raise :class:`UndeclaredArtifact` on ``UNKNOWN``."""
        if self.entry is None:
            raise UndeclaredArtifact(
                f"{self.artifact} has not been declared in any loaded source, so "
                "its control status is unknown, not clear. The declared set is "
                "incomplete by construction; ask the control office rather than "
                "inferring from this absence"
            )
        return self.entry


# ---------------------------------------------------------------------------
# Declaration sources.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Declarations:
    """Entries from one named source, with the origin kept alongside.

    The origin is not part of an entry, so the same declaration from two files
    compares equal and composes without conflict.
    """

    origin: str
    entries: tuple[ControlledCodeEntry, ...] = ()

    @classmethod
    def from_entries(cls, entries: Iterable[ControlledCodeEntry], *, origin: str) -> Declarations:
        """Build a source that is not a file, e.g. entries fetched at runtime."""
        if not origin or not origin.strip():
            raise ControlledCodeError(
                "a declaration source must name its origin, so a later conflict "
                "can say where each side came from"
            )
        return cls(origin=origin, entries=tuple(entries))


_TOP_LEVEL_KEYS = frozenset({"schema_version", "code"})
_ENTRY_KEYS = frozenset({"artifact", "status", "basis", "access_rule", "classification"})
_BASIS_KEYS = frozenset({"citation", "determined_by", "regime", "determined_on", "note"})
_RULE_KEYS = frozenset({"enclave", "authorized_persons_ref", "note"})

#: Keys somebody reaches for when they believe data is export-controlled. They
#: get the doctrine back instead of a bare "unknown key".
_DOCTRINE_MISTAKE_KEYS = frozenset(
    {
        "controlled_data",
        "data",
        "data_classification",
        "data_may_egress",
        "inputs",
        "inputs_and_outputs_may_egress",
        "outputs",
        "output_classification",
        "results",
        "results_controlled",
    }
)

_DOCTRINE = (
    "export control attaches to the code itself; its inputs, outputs and "
    "results are not export-controlled and cannot be declared here. Data may "
    "still be access-controlled for proprietary or personal-information "
    "reasons, which is a different regime and is not set in this registry"
)


def _reject_unknown_keys(where: str, raw: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(raw) - allowed)
    if not unknown:
        return
    doctrine = [key for key in unknown if key in _DOCTRINE_MISTAKE_KEYS]
    if doctrine:
        raise ManifestError(f"{where}: {', '.join(doctrine)} is not a key; {_DOCTRINE}")
    raise ManifestError(
        f"{where}: unknown key(s) {', '.join(unknown)}; "
        f"expected one of {', '.join(sorted(allowed))}"
    )


def _require_table(where: str, value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{where}: [{name}] must be a table, got {type(value).__name__}")
    return value


def _require_str(where: str, raw: Mapping[str, Any], key: str) -> str:
    if key not in raw:
        raise ManifestError(f"{where}: missing required key {key!r}")
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where}: {key!r} must be a non-empty string, got {value!r}")
    return value


def _optional_str(where: str, raw: Mapping[str, Any], key: str) -> str | None:
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, str):
        raise ManifestError(f"{where}: {key!r} must be a string, got {value!r}")
    return value


def _parse_basis(where: str, raw: Mapping[str, Any]) -> ControlBasis:
    _reject_unknown_keys(f"{where} [code.basis]", raw, _BASIS_KEYS)
    determined_on = raw.get("determined_on")
    if determined_on is not None and not isinstance(determined_on, date):
        raise ManifestError(
            f"{where} [code.basis]: 'determined_on' must be a TOML date "
            f"(YYYY-MM-DD, unquoted), got {determined_on!r}"
        )
    return ControlBasis(
        citation=_require_str(f"{where} [code.basis]", raw, "citation"),
        determined_by=_require_str(f"{where} [code.basis]", raw, "determined_by"),
        regime=_optional_str(f"{where} [code.basis]", raw, "regime"),
        determined_on=determined_on,
        note=_optional_str(f"{where} [code.basis]", raw, "note"),
    )


def _parse_access_rule(where: str, raw: Mapping[str, Any]) -> AccessRule:
    _reject_unknown_keys(f"{where} [code.access_rule]", raw, _RULE_KEYS)
    return AccessRule(
        enclave=_require_str(f"{where} [code.access_rule]", raw, "enclave"),
        authorized_persons_ref=_optional_str(
            f"{where} [code.access_rule]", raw, "authorized_persons_ref"
        ),
        note=_optional_str(f"{where} [code.access_rule]", raw, "note"),
    )


def _parse_entry(source: str, index: int, raw: Mapping[str, Any]) -> ControlledCodeEntry:
    label = raw.get("artifact") if isinstance(raw.get("artifact"), str) else None
    where = f"{source}: {label}" if label else f"{source}: [[code]] entry {index + 1}"

    _reject_unknown_keys(where, raw, _ENTRY_KEYS)
    artifact_uri = _require_str(where, raw, "artifact")
    status_raw = _require_str(where, raw, "status")
    try:
        status = ControlStatus(status_raw)
    except ValueError as exc:
        candidates = ", ".join(member.value for member in ControlStatus)
        raise ManifestError(
            f"{where}: unknown status {status_raw!r}; candidates: {candidates}"
        ) from exc

    classification_raw = _optional_str(where, raw, "classification")
    classification: Classification | None = None
    if classification_raw is not None:
        try:
            classification = Classification.from_str(classification_raw)
        except ValueError as exc:
            raise ManifestError(f"{where}: {exc}") from exc

    if "basis" not in raw:
        raise ManifestError(f"{where}: missing required table [code.basis]")
    basis = _parse_basis(where, _require_table(where, raw["basis"], "code.basis"))

    access_rule = None
    if "access_rule" in raw:
        access_rule = _parse_access_rule(
            where, _require_table(where, raw["access_rule"], "code.access_rule")
        )

    try:
        return ControlledCodeEntry(
            artifact=_as_artifact(artifact_uri),
            status=status,
            basis=basis,
            access_rule=access_rule,
            classification=classification,
        )
    except ControlledCodeError as exc:
        raise ManifestError(f"{where}: {exc}") from exc


def load_manifest(path: Path | str) -> Declarations:
    """Read one manifest into :class:`Declarations`, or raise.

    Every failure raises :class:`ManifestError` with the path first and the
    problem after it. A manifest is never allowed to degrade into an empty
    result: a site that declared nothing and a file that would not parse must
    stay distinguishable.

    A manifest that parses and declares no entries is a valid, empty source.
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ManifestError(f"{source}: manifest not found") from exc
    except OSError as exc:
        raise ManifestError(f"{source}: cannot read manifest ({exc})") from exc

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{source}: invalid TOML ({exc})") from exc

    _reject_unknown_keys(str(source), data, _TOP_LEVEL_KEYS)

    if "schema_version" not in data:
        raise ManifestError(
            f"{source}: missing required key 'schema_version' "
            f"(this build understands {SCHEMA_VERSION})"
        )
    version = data["schema_version"]
    if version != SCHEMA_VERSION:
        raise ManifestError(
            f"{source}: unsupported schema_version {version!r}; "
            f"this build understands {SCHEMA_VERSION}"
        )

    raw_entries = data.get("code", [])
    if not isinstance(raw_entries, list):
        raise ManifestError(
            f"{source}: 'code' must be an array of tables ([[code]]), "
            f"got {type(raw_entries).__name__}"
        )

    entries = []
    for index, raw in enumerate(raw_entries):
        entries.append(_parse_entry(str(source), index, _require_table(str(source), raw, "code")))
    return Declarations(origin=str(source), entries=tuple(entries))


# ---------------------------------------------------------------------------
# The registry.
# ---------------------------------------------------------------------------


class ControlledCodeRegistry:
    """Composed declarations, queried by artifact. Read-only once built.

    Construct through :meth:`empty`, :meth:`compose` or :meth:`from_manifests`.
    An empty registry is a perfectly valid state and answers ``UNKNOWN`` to
    everything, which is different from answering "nothing is controlled".
    """

    __slots__ = ("_by_artifact", "_sources")

    def __init__(
        self,
        by_artifact: Mapping[ResourceRef, tuple[ControlledCodeEntry, str]],
        sources: tuple[str, ...],
    ) -> None:
        self._by_artifact = dict(by_artifact)
        self._sources = sources

    @classmethod
    def empty(cls) -> ControlledCodeRegistry:
        """A registry with no declarations. Answers ``UNKNOWN`` to everything."""
        return cls({}, ())

    @classmethod
    def compose(cls, *declarations: Declarations) -> ControlledCodeRegistry:
        """Compose declaration sources, refusing any disagreement between them.

        Identical redeclaration of an artifact is accepted and collapses to one
        entry. Any difference raises :class:`ConflictingDeclaration` naming the
        artifact, both origins and the differing fields.
        """
        composed: dict[ResourceRef, tuple[ControlledCodeEntry, str]] = {}
        for declaration in declarations:
            for entry in declaration.entries:
                existing = composed.get(entry.artifact)
                if existing is None:
                    composed[entry.artifact] = (entry, declaration.origin)
                    continue
                held, held_origin = existing
                if held != entry:
                    raise ConflictingDeclaration(
                        _conflict_message(
                            entry.artifact, held, held_origin, entry, declaration.origin
                        )
                    )
        sources = tuple(dict.fromkeys(d.origin for d in declarations))
        return cls(composed, sources)

    @classmethod
    def from_manifests(cls, *paths: Path | str) -> ControlledCodeRegistry:
        """Load and compose manifest files. No paths means an empty registry."""
        return cls.compose(*(load_manifest(path) for path in paths))

    @property
    def entries(self) -> tuple[ControlledCodeEntry, ...]:
        """Every composed entry, in first-declared order."""
        return tuple(entry for entry, _ in self._by_artifact.values())

    @property
    def sources(self) -> tuple[str, ...]:
        """Every origin that contributed, in the order they were composed."""
        return self._sources

    def lookup(self, artifact: ResourceRef | str) -> ControlFinding:
        """Answer for one artifact. Accepts a ref, a ``code://`` URI or a name.

        An artifact no source has declared returns ``UNKNOWN``. That is the
        ordinary answer while the declared set is incomplete, and it is not a
        statement that the artifact is uncontrolled.
        """
        ref = _as_artifact(artifact)
        found = self._by_artifact.get(ref)
        if found is None:
            return ControlFinding(artifact=ref, standing=ControlStanding.UNKNOWN)
        entry, origin = found
        standing = (
            ControlStanding.CONTROLLED
            if entry.status is ControlStatus.CONTROLLED
            else ControlStanding.NOT_CONTROLLED
        )
        return ControlFinding(artifact=ref, standing=standing, entry=entry, source=origin)

    def __len__(self) -> int:
        return len(self._by_artifact)

    def __repr__(self) -> str:
        return (
            f"ControlledCodeRegistry({len(self._by_artifact)} entries "
            f"from {len(self._sources)} source(s))"
        )


def _conflict_message(
    artifact: ResourceRef,
    held: ControlledCodeEntry,
    held_origin: str,
    incoming: ControlledCodeEntry,
    incoming_origin: str,
) -> str:
    differing = ", ".join(
        f.name
        for f in fields(ControlledCodeEntry)
        if getattr(held, f.name) != getattr(incoming, f.name)
    )
    return (
        f"{artifact} is declared differently by {held_origin} and "
        f"{incoming_origin}: the declarations disagree on {differing}. "
        "Refusing to compose; reconcile the sources or drop one, because "
        "picking a winner here would silently change what is controlled"
    )


__all__ = [
    "CODE_SCHEME",
    "SCHEMA_VERSION",
    "AccessRule",
    "ConflictingDeclaration",
    "ControlBasis",
    "ControlFinding",
    "ControlStanding",
    "ControlStatus",
    "ControlledCodeEntry",
    "ControlledCodeError",
    "ControlledCodeRegistry",
    "Declarations",
    "ManifestError",
    "UndeclaredArtifact",
    "code_ref",
    "load_manifest",
]
