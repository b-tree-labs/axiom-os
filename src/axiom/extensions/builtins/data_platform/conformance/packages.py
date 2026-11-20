# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""How a deposit is packaged: open it, name its parts, and keep it harmless.

Bronze keeps a deposit byte for byte (ADR-194), so a tar of a day's export stays
a tar. Unpacking is therefore a conform step, and it is the same step for every
partner and every format. This module owns it, so a reader only ever sees named
parts and never learns whether they arrived compressed, bundled or loose.

Two small provider kinds, composed by name (``tar.bz2`` is a tar in a bz2):

- a **codec** undoes compression (``bz2``, ``gz``, ``xz``);
- a **container** lists what is inside (``tar``, ``zip``).

``auto`` (the default) decides from the file's own bytes, never from its name,
because a name is the part a producer changes without telling anyone. A
:class:`Layout` then says which parts play which role (``samples``,
``catalogue``), as declared data.

A DEPOSIT IS UNTRUSTED INPUT. It comes from outside the platform, and one conform
process reads every tenant's bronze. So, stated once and held by the tests:

- **Nothing is written to disk.** Parts are read as streams and member names are
  only ever matched, never used as a path, so a hostile name reaches no file.
- **Expansion is bounded.** Decompressed bytes are metered against
  :class:`Limits` as they are produced, including while a container is scanned,
  so a small file that expands without end is stopped, not survived.
- **Only plain files are parts.** A link, a device, an absolute path, a ``..``
  segment or an encrypted member refuses the whole package.
- **One level.** A package inside a package stays an opaque part.
- **Refusing is loud.** :class:`UnsafePackage` is an error in the conform funnel
  and fails a strict run. It is never a quiet decline.
- **Declarations are data.** A layout or an opener string can only name a
  provider already registered. New provider code arrives through the entry-point
  gate below and nowhere else: importing is execution, so it is open only to
  portfolio distributions (ADR-023-A1 A1.3).
"""

from __future__ import annotations

import bz2
import fnmatch
import gzip
import io
import logging
import lzma
import stat
import tarfile
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Protocol, TextIO

from .discovery import PACKAGE_GROUP, register_discovered

log = logging.getLogger(__name__)

AUTO = "auto"
PLAIN = "plain"
#: How much of a stream a container needs to recognise itself (a tar says so at 257).
_SNIFF_BYTES = 512
_GIB = 1024**3


class UnsafePackage(ValueError):
    """This deposit will not be opened, and a person should know why."""


class UnknownOpener(ValueError):
    """A declaration named an opener nothing has registered."""


@dataclass(frozen=True)
class Limits:
    """What one deposit may cost. Raise it for a site with reason to, per lane."""

    max_expanded_bytes: int = 16 * _GIB
    max_members: int = 4096


DEFAULT_LIMITS = Limits()


@dataclass
class _Budget:
    limit: int
    spent: int = 0

    def spend(self, n: int) -> None:
        self.spent += n
        if self.spent > self.limit:
            raise UnsafePackage(
                f"package expands past its limit of {self.limit} bytes; refused unread "
                f"(raise the lane's limit only if this size is expected)"
            )


class _Bounded(io.RawIOBase):
    """A decompressing stream that refuses to go past a position.

    A container is scanned by seeking, and seeking in a compressed stream IS
    decompression, so the cap has to sit under the scan, not only under the reads.
    """

    def __init__(self, inner: BinaryIO, limit: int) -> None:
        super().__init__()
        self._inner = inner
        self._limit = limit

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._inner.tell()

    def readinto(self, b: Any) -> int:
        data = self._inner.read(len(b))
        if self._inner.tell() > self._limit:
            raise UnsafePackage(
                f"package expands past its limit of {self._limit} bytes; refused unread"
            )
        b[: len(data)] = data
        return len(data)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_CUR:
            offset += self._inner.tell()
        elif whence != io.SEEK_SET:
            raise io.UnsupportedOperation("a decompressing stream has no cheap end")
        if offset > self._limit:
            raise UnsafePackage(
                f"package claims content at byte {offset}, past its limit of "
                f"{self._limit} bytes; refused unread"
            )
        return self._inner.seek(offset)

    def close(self) -> None:
        self._inner.close()
        super().close()


class _Metered(io.RawIOBase):
    """Counts what is actually read, whatever a header claimed."""

    def __init__(self, inner: BinaryIO, budget: _Budget) -> None:
        super().__init__()
        self._inner = inner
        self._budget = budget

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        data = self._inner.read(len(b))
        self._budget.spend(len(data))
        b[: len(data)] = data
        return len(data)


@dataclass(frozen=True)
class Entry:
    """One plain file a container found."""

    name: str
    open: Callable[[], BinaryIO]
    size: int | None = None


@dataclass(frozen=True)
class Codec:
    """Undoes one compression. ``suffixes`` only tidy a part's name, never select."""

    name: str
    magic: bytes
    open: Callable[[Path], BinaryIO]
    suffixes: tuple[str, ...] = ()


class Container(Protocol):
    name: str

    def sniff(self, head: bytes) -> bool:
        """Do these first bytes say this is my format?"""

    def entries(self, stream: BinaryIO, limits: Limits) -> list[Entry]:
        """Every plain file inside, refusing anything else (:func:`checked_name`)."""


def checked_name(name: str) -> str:
    """A member's name as a relative path, or a refusal.

    The name is never used to touch a file. A package that carries one of these
    anyway did not come from an instrument, so it is refused rather than tidied.
    """
    clean = name.replace("\\", "/")
    parts = PurePosixPath(clean).parts
    if (
        "\0" in clean
        or clean.startswith("/")
        or ".." in parts
        or ":" in (parts[0] if parts else "")
    ):
        raise UnsafePackage(f"package member {name!r} is not a plain relative path")
    return clean


class TarContainer:
    name = "tar"

    def sniff(self, head: bytes) -> bool:
        return len(head) > 262 and head[257:262] == b"ustar"

    def entries(self, stream: BinaryIO, limits: Limits) -> list[Entry]:
        tar = tarfile.open(fileobj=stream, mode="r:")
        found: list[Entry] = []
        seen = 0
        for info in tar:  # lazily, so the member cap stops the scan itself
            seen += 1
            if seen > limits.max_members:
                raise UnsafePackage(f"package holds more than {limits.max_members} members")
            if info.isdir():
                continue
            if not info.isreg():
                raise UnsafePackage(f"package member {info.name!r} is not a plain file")
            found.append(
                Entry(
                    name=checked_name(info.name),
                    size=info.size,
                    open=lambda member=info: tar.extractfile(member),
                )
            )
        return found


class ZipContainer:
    name = "zip"

    def sniff(self, head: bytes) -> bool:
        return head[:4] in (b"PK\x03\x04", b"PK\x05\x06")

    def entries(self, stream: BinaryIO, limits: Limits) -> list[Entry]:
        if isinstance(stream, _Bounded):
            raise UnsafePackage("a zip inside a compressed stream is not a package we read")
        archive = zipfile.ZipFile(stream)
        infos = archive.infolist()
        if len(infos) > limits.max_members:
            raise UnsafePackage(f"package holds more than {limits.max_members} members")
        found: list[Entry] = []
        for info in infos:
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise UnsafePackage(f"package member {info.filename!r} is encrypted")
            kind = stat.S_IFMT(info.external_attr >> 16)  # 0 when the writer set no type
            if kind not in (0, stat.S_IFREG):
                raise UnsafePackage(f"package member {info.filename!r} is not a plain file")
            found.append(
                Entry(
                    name=checked_name(info.filename),
                    size=info.file_size,
                    open=lambda member=info: archive.open(member),
                )
            )
        return found


class OpenerRegistry:
    """Codecs and containers by name. A deployment adds to it; a deposit cannot."""

    def __init__(self) -> None:
        self._codecs: dict[str, Codec] = {}
        self._containers: dict[str, Container] = {}

    def register_codec(self, codec: Codec) -> None:
        if codec.name in self._codecs or codec.name in (AUTO, PLAIN):
            raise ValueError(f"codec {codec.name!r} is already registered")
        self._codecs[codec.name] = codec

    def register_container(self, container: Container) -> None:
        if container.name in self._containers or container.name in (AUTO, PLAIN):
            raise ValueError(f"container {container.name!r} is already registered")
        self._containers[container.name] = container

    def names(self) -> list[str]:
        return sorted({AUTO, PLAIN, *self._codecs, *self._containers})

    def declared(self, opener: str) -> tuple[Container | None, Codec | None]:
        """``tar.bz2`` -> (tar, bz2). Refuses a name nothing registered."""
        if opener == PLAIN:
            return None, None
        first, _, rest = opener.partition(".")
        if not rest and first in self._codecs:
            return None, self._codecs[first]
        container = self._containers.get(first)
        codec = self._codecs.get(rest) if rest else None
        if container is None or (rest and codec is None):
            raise UnknownOpener(f"no opener {opener!r}; registered: {', '.join(self.names())}")
        return container, codec

    def loose_name(self, name: str) -> str:
        """``name`` less any compression suffix, so a sibling matches like a part."""
        for codec in self._codecs.values():
            stripped = _unsuffixed(name, codec)
            if stripped != name:
                return stripped
        return name

    def sniff_codec(self, head: bytes) -> Codec | None:
        return next((c for c in self._codecs.values() if head.startswith(c.magic)), None)

    def sniff_container(self, head: bytes) -> Container | None:
        return next((c for c in self._containers.values() if c.sniff(head)), None)


def _builtin_openers() -> OpenerRegistry:
    registry = OpenerRegistry()
    registry.register_codec(Codec("bz2", b"BZh", bz2.open, (".bz2", ".bzip2")))
    registry.register_codec(Codec("gz", b"\x1f\x8b", gzip.open, (".gz", ".gzip")))
    registry.register_codec(Codec("xz", b"\xfd7zXZ\x00", lzma.open, (".xz",)))
    registry.register_container(TarContainer())
    registry.register_container(ZipContainer())
    return registry


@lru_cache(maxsize=1)
def default_openers() -> OpenerRegistry:
    """The built-ins, plus what portfolio distributions contribute."""
    registry = _builtin_openers()
    register_discovered(registry, group=PACKAGE_GROUP)
    return registry


@dataclass(frozen=True)
class Member:
    """One readable part of a package."""

    name: str
    size: int | None
    _open: Callable[[], BinaryIO] = field(repr=False)
    _budget: _Budget = field(repr=False)

    @property
    def basename(self) -> str:
        return PurePosixPath(self.name).name

    def binary(self) -> io.BufferedReader:
        return io.BufferedReader(_Metered(self._open(), self._budget))

    @contextmanager
    def text(self, encoding: str = "utf-8-sig") -> Iterator[TextIO]:
        with io.TextIOWrapper(self.binary(), encoding=encoding, newline="") as handle:
            yield handle


def _unsuffixed(name: str, codec: Codec | None) -> str:
    if codec is not None:
        for suffix in codec.suffixes:
            if name.lower().endswith(suffix):
                return name[: -len(suffix)]
    return name


def _single(stream: BinaryIO, name: str, codec: Codec | None) -> Entry:
    def rewind() -> BinaryIO:
        stream.seek(0)
        return stream

    return Entry(name=_unsuffixed(name, codec), open=rewind)


@contextmanager
def open_package(
    path: Path | str,
    *,
    name: str = "",
    opener: str = AUTO,
    limits: Limits = DEFAULT_LIMITS,
    registry: OpenerRegistry | None = None,
) -> Iterator[list[Member]]:
    """The plain files in this deposit: one for a loose or compressed file, or many.

    ``name`` is the deposit's own name, which a loose file's part keeps (less any
    compression suffix). Members are valid inside the ``with`` block only.
    """
    openers = registry or default_openers()
    path = Path(path)
    name = name or path.name
    container: Container | None
    codec: Codec | None
    if opener == AUTO:
        with path.open("rb") as raw:
            codec = openers.sniff_codec(raw.read(16))
        container = None
    else:
        container, codec = openers.declared(opener)

    stream: BinaryIO = (
        path.open("rb") if codec is None else _Bounded(codec.open(path), limits.max_expanded_bytes)  # type: ignore[assignment]
    )
    try:
        if opener == AUTO:
            head = stream.read(_SNIFF_BYTES)
            stream.seek(0)
            container = openers.sniff_container(head)
        entries = (
            container.entries(stream, limits)
            if container is not None
            else [_single(stream, name, codec)]
        )
        budget = _Budget(limits.max_expanded_bytes)
        yield [Member(e.name, e.size, e.open, budget) for e in entries]
    finally:
        stream.close()


@dataclass(frozen=True)
class Layout:
    """Which parts of a deposit play which role, as declared data.

    ``{"samples": ("*samples.csv",), "catalogue": ("*channels.csv",)}``. Patterns
    are shell-style, match the part's base name, and ignore case. Hidden files
    are never a part (an archiver's ``._name`` shadow matches any pattern).
    """

    members: Mapping[str, tuple[str, ...]]

    @classmethod
    def declared(cls, raw: Mapping[str, Any]) -> Layout:
        """A layout from configuration, refusing a shape it would misread."""
        members: dict[str, tuple[str, ...]] = {}
        for role, patterns in raw.items():
            if isinstance(patterns, str):
                patterns = [patterns]
            if not role or not patterns or not all(isinstance(p, str) and p for p in patterns):
                raise ValueError(f"layout role {role!r} needs at least one name pattern")
            if any("/" in p or "\\" in p for p in patterns):
                raise ValueError(f"layout role {role!r}: patterns match a base name, not a path")
            members[role] = tuple(patterns)
        if not members:
            raise ValueError("a layout declares at least one role")
        return cls(members=members)

    def role_of(self, name: str) -> str | None:
        base = PurePosixPath(name.replace("\\", "/")).name
        if base.startswith("."):
            return None
        for role, patterns in self.members.items():
            if any(fnmatch.fnmatchcase(base.lower(), p.lower()) for p in patterns):
                return role
        return None


class Parts:
    """A deposit's parts by role. What a reader is handed instead of a path."""

    def __init__(
        self,
        deposit: Any,
        members: Sequence[Member],
        layout: Layout,
        limits: Limits,
        registry: OpenerRegistry | None,
    ) -> None:
        self.deposit = deposit
        self.members = tuple(members)
        self.layout = layout
        self._limits = limits
        self._registry = registry

    def all(self, role: str) -> list[Member]:
        """Every part of this deposit in ``role``, in the package's own order."""
        return [m for m in self.members if self.layout.role_of(m.name) == role]

    @contextmanager
    def one(self, role: str) -> Iterator[Member | None]:
        """The single part in ``role``, from this package first, then the folder.

        A bundle that carries its own catalogue is read with THAT catalogue: a
        file of the same role beside it belongs to something else. Several
        candidates at the same level raise, because guessing is how the wrong
        catalogue gets joined to a day's samples.
        """
        inside = self.all(role)
        if len(inside) > 1:
            names = ", ".join(m.name for m in inside)
            raise ValueError(f"{self.deposit.name}: {len(inside)} parts are {role!r} ({names})")
        if inside:
            yield inside[0]
            return
        openers = self._registry or default_openers()
        sibling = self.deposit.companion(
            lambda d: self.layout.role_of(openers.loose_name(d.name)) == role
        )
        if sibling is None:
            yield None
            return
        with open_package(
            sibling.path, name=sibling.name, limits=self._limits, registry=self._registry
        ) as members:
            if len(members) != 1:
                raise ValueError(f"{sibling.name}: a {role!r} beside a deposit must be one file")
            yield members[0]


@contextmanager
def open_parts(
    deposit: Any,
    layout: Layout,
    *,
    opener: str = AUTO,
    limits: Limits = DEFAULT_LIMITS,
    registry: OpenerRegistry | None = None,
) -> Iterator[Parts]:
    """Open ``deposit`` and hand back its parts by role."""
    with open_package(
        deposit.path, name=deposit.name, opener=opener, limits=limits, registry=registry
    ) as members:
        yield Parts(deposit, members, layout, limits, registry)


__all__ = [
    "AUTO",
    "DEFAULT_LIMITS",
    "PLAIN",
    "Codec",
    "Container",
    "Entry",
    "Layout",
    "Limits",
    "Member",
    "OpenerRegistry",
    "Parts",
    "TarContainer",
    "UnknownOpener",
    "UnsafePackage",
    "ZipContainer",
    "checked_name",
    "default_openers",
    "open_package",
    "open_parts",
]
