# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``VideoSource`` and the source-kind registry (ADR-240 D1, spec-video §VideoSource).

A video source is a connector of kind ``video``; ``params.source.kind`` names what
it is, and this registry turns that name into a :class:`VideoSource`. A kind
declares whether it is *ephemeral* (live: a lost frame is gone) or *replayable*,
the flags ``instrument video add`` offers for it, and a factory taking ``(source_params,
credential)``. Everything a kind does not declare (the bus, the claim, status,
triggers) is held once by the extension, so a new kind is one registration that
gets the rest for free.

The registry is the platform's one kind registry (ADR-110 §1): a duplicate is
loud, re-registering the same entry is harmless, and an unknown kind names the
known ones.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from axiom.infra.connector_registry import ConnectorRegistry, UnknownConnectorKind

if TYPE_CHECKING:
    from axiom.extensions.builtins.connector.detect import DetectResult


class VideoSourceError(Exception):
    """A source could not do what was asked. ``code`` classifies it; ``fix`` is the exact remedy.

    Codes: ``invalid-config``, ``ffmpeg-missing``, ``unreachable``, ``auth-failed``,
    ``not-found``, ``codec-unsupported``, ``no-frame``, ``timeout``,
    ``credential-unresolved``, ``not-declared``, ``error``.
    """

    def __init__(self, message: str, *, code: str = "error", fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.fix = fix


class InvalidSourceConfig(VideoSourceError, ValueError):
    """The declaration itself is wrong; nothing was contacted."""

    def __init__(self, message: str, *, fix: str = "") -> None:
        super().__init__(message, code="invalid-config", fix=fix)


class UnknownSourceKind(UnknownConnectorKind):
    """No kind is registered under that name. Both a KeyError and a ValueError."""


@dataclass(frozen=True)
class VerifyReport:
    """What one frame proved: codec, size, frame rate, and how long the first frame took."""

    codec: str
    width: int
    height: int
    fps: float | None
    latency_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "latency_ms": self.latency_ms,
        }


@runtime_checkable
class VideoSource(Protocol):
    """One source of frames. The capture service decodes it through ``ffmpeg``."""

    kind: str
    replayable: bool

    def detect(self) -> DetectResult:
        """ADR-068 detection; ``details["steps"]`` lists the rungs diagnose climbs for this kind."""
        ...

    def verify(self) -> VerifyReport:
        """One frame, and what it showed. Raises :class:`VideoSourceError` with a fix."""
        ...

    def ffmpeg_input_args(self) -> list[str]:
        """What goes before ``ffmpeg``'s output options, the ``-i <input>`` included.

        A kind whose input carries a secret names ``ffmpeg.SECRET_INPUT`` here and
        returns the secret part from ``ffmpeg_secret_input()`` (optional; ``None`` or
        absent means there is none), so the secret is never on a command line.
        """
        ...

    def redacted(self) -> dict[str, Any]:
        """What ``status`` may print: never a credential."""
        ...


SourceFactory = Callable[[Mapping[str, Any], Any], VideoSource]


@dataclass(frozen=True)
class SourceParam:
    """One parameter a kind reads from ``[params.source]``; ``instrument video add`` offers it as a flag."""

    name: str
    help: str
    required: bool = False
    default: str | None = None
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError(
                f"a source parameter needs a name of letters, digits and _, got {self.name!r}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "help": self.help,
            "required": self.required,
            "default": self.default,
            "choices": list(self.choices),
        }


@dataclass(frozen=True)
class SourceKind:
    """A registered kind: its name, whether it is replayable, its parameters and factory."""

    kind: str
    replayable: bool
    factory: SourceFactory
    params: tuple[SourceParam, ...] = ()
    description: str = ""

    def build(self, source_params: Mapping[str, Any], credential: Any = None) -> VideoSource:
        """Build an instance; ``kind`` is removed from the params first."""
        params = {k: v for k, v in dict(source_params).items() if k != "kind"}
        return self.factory(params, credential)

    def validate(self, source_params: Mapping[str, Any]) -> None:
        """Raise :class:`InvalidSourceConfig` if these params could never work. Contacts nothing.

        A kind's factory refuses bad params at construction, so validating is
        building without a credential (a source with no login is a valid source).
        """
        given = dict(source_params)
        missing = [p.name for p in self.params if p.required and not given.get(p.name)]
        if missing:
            raise InvalidSourceConfig(
                f"source kind {self.kind!r} needs {', '.join(missing)}",
                fix=f"set {', '.join(missing)} under [connector.params.source]",
            )
        self.build(given, None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "replayable": self.replayable,
            "description": self.description,
            "params": [p.as_dict() for p in self.params],
        }


_KINDS: ConnectorRegistry[SourceKind] = ConnectorRegistry("video source kind")


def register_source_kind(kind: SourceKind, *, replace: bool = False) -> None:
    _KINDS.register(kind.kind, kind, replace=replace)


def unregister_source_kind(name: str) -> None:
    _KINDS.unregister(name)


def source_kind(name: str) -> SourceKind:
    """The registered kind, or :class:`UnknownSourceKind` naming the known ones."""
    try:
        return _KINDS.get(name)
    except UnknownConnectorKind as exc:
        raise UnknownSourceKind(str(exc)) from None


def source_kinds() -> list[SourceKind]:
    """Every registered kind, sorted by name."""
    return [_KINDS.get(name) for name in _KINDS.available()]


def create_source(
    kind: str, source_params: Mapping[str, Any], credential: Any = None
) -> VideoSource:
    """Dispatch on ``kind`` and build the source."""
    return source_kind(kind).build(source_params, credential)


__all__ = [
    "InvalidSourceConfig",
    "SourceFactory",
    "SourceKind",
    "SourceParam",
    "UnknownSourceKind",
    "VerifyReport",
    "VideoSource",
    "VideoSourceError",
    "create_source",
    "register_source_kind",
    "source_kind",
    "source_kinds",
    "unregister_source_kind",
]
