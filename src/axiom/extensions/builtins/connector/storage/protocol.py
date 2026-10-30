# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``StorageConnectorProvider`` Protocol — ADR-062.

Four-capability surface every storage vendor (Box, OneDrive, SharePoint,
S3-compat, …) implements. Vendors declare absent capabilities via the
``capabilities`` frozenset rather than raising at call time, so the
wizard and ``axi connector status`` can show honest support matrices.
"""

from __future__ import annotations

import enum
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class StorageCapability(enum.Enum):
    """Per-vendor capability presence (ADR-062 §Decision)."""

    LIST = "list"
    READ = "read"
    WRITE = "write"
    WATCH = "watch"
    REPLY_INGEST = "reply_ingest"
    # ADR-220 §5: what vendors differ on beyond the four ADR-062 verbs.
    CHANGE_CURSOR = "change_cursor"  # a listed file carries an etag or cursor to compare
    CHECKSUM = "checksum"  # a listed file carries the vendor's content hash
    VERSIONING = "versioning"  # earlier versions of a file stay readable
    CONDITIONAL_PUT = "conditional_put"  # write only if absent, or only if unchanged
    RANGED_READ = "ranged_read"  # read a byte range without the whole object
    LARGE_UPLOAD = "large_upload"  # chunked upload sessions for big files
    LOGIN = "login"  # an interactive or app-consent login flow


class MissingCapability(LookupError):
    """A caller asked a connector for something its provider does not declare."""


def require(provider: Any, needed: set[StorageCapability] | frozenset[StorageCapability],
            *, connector: str = "") -> None:
    """Refuse BEFORE the call when ``provider`` lacks any of ``needed``.

    The answer names the connector, its kind and the gap, so a caller never
    branches on a vendor to find out what it can do (ADR-220 §5).
    """
    have = frozenset(getattr(provider, "capabilities", frozenset()))
    gap = sorted(c.value for c in set(needed) - have)
    if gap:
        who = connector or getattr(provider, "name", "") or "this connector"
        raise MissingCapability(
            f"connector {who!r} (kind {getattr(provider, 'vendor', '?')!r}) does not support: "
            f"{', '.join(gap)}"
        )


# ---------------------------------------------------------------------------
# Reference types — kept dataclass-thin so vendors don't subclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileRef:
    """Vendor-agnostic file pointer.

    ``vendor`` identifies which connector minted the ref (so a federated
    ``axiom://`` URI can round-trip back to the right provider).
    ``path`` is vendor-native (Box folder/file id, OneDrive item id,
    SharePoint server-relative URL). ``hints`` carries optional bits
    (mime, etag) the provider may set or ignore.
    """

    vendor: str
    path: str
    hints: dict[str, Any] | None = None


@dataclass(frozen=True)
class FileContent:
    """Bytes + minimal metadata; large files stream via ``stream`` if
    set (a callable returning an iterator of byte chunks), and ``data``
    stays ``None``. Adapters pick one — never both."""

    data: bytes | None = None
    stream: Any = None
    media_type: str | None = None


@dataclass(frozen=True)
class PutReceipt:
    """Result of a successful ``put_file``. ``ref`` is the
    post-write canonical ref (may differ from the requested one if the
    vendor renamed for collision). ``version`` is the vendor-native version
    identifier of the stored bytes when available (an etag, a generation
    number, an mtime), else ``None``.

    ``version`` is the one name the platform uses for "which stored bytes"
    (ADR-112 §D4): the same field name as ``infra.storage.UploadResult.version``
    and the mirror's ``RemoteDoc.version``. The two storage *families* stay
    separate (the DRY floor); only the field name is shared."""

    ref: FileRef
    version: str | None = None
    bytes_written: int = 0


@dataclass(frozen=True)
class ListParams:
    """List a folder / container.

    ``folder`` is vendor-native (Box folder id, etc.). ``recursive`` is
    advisory — vendors that can't recurse natively return only the top
    level and set ``hints['recursive_supported'] = False`` on emitted
    refs."""

    folder: str
    recursive: bool = False
    max_items: int | None = None


@dataclass(frozen=True)
class WatchParams:
    """Subscribe to a vendor's change feed for a folder.

    ``delivery`` is one of ``"webhook"`` (vendor pushes) or ``"poll"``
    (we pull). Vendors decide which they support; if neither, watch
    isn't in ``capabilities``."""

    folder: str
    delivery: str = "webhook"


@dataclass(frozen=True)
class WatchHandle:
    """Returned from ``start_watch``; the operator-visible record. The
    bus event subject is ``connector.<vendor>.file_landed`` per
    ADR-062; the watch_id lets the operator stop the watch later."""

    watch_id: str
    vendor: str
    folder: str
    delivery: str


@dataclass(frozen=True)
class ReplyParams:
    """Pull replies / comments on watched files.

    ``since`` filters by vendor-native cursor (Box comment id, M365
    delta token, …). ``folder`` scopes the pull to one container."""

    folder: str
    since: str | None = None


@dataclass(frozen=True)
class ReplyEvent:
    """A comment or reply on a vendor file. The bus event subject is
    ``connector.<vendor>.reply``; HERALD's reply-routing binds it back
    to the originating ``ActionEnvelope`` if ``in_reply_to`` matches."""

    vendor: str
    file: FileRef
    body: str
    author: str
    posted_at: str
    in_reply_to: str | None = None


# ---------------------------------------------------------------------------
# The Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StorageConnectorProvider(Protocol):
    """Four-capability storage contract.

    Implementers expose ``vendor`` (str) + ``capabilities`` (frozenset).
    Methods whose capability is absent may raise ``NotImplementedError``
    — but a well-behaved adapter just omits the capability from the set
    so the wizard never invites the operator to enable it.
    """

    vendor: str
    capabilities: frozenset[StorageCapability]

    def list_files(self, params: ListParams) -> Iterator[FileRef]: ...

    def get_file(self, ref: FileRef) -> FileContent: ...

    def put_file(self, ref: FileRef, content: FileContent) -> PutReceipt: ...

    def start_watch(self, params: WatchParams) -> WatchHandle: ...

    def ingest_replies(self, params: ReplyParams) -> Iterator[ReplyEvent]: ...
