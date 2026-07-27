# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for ADR-062 storage connector surface.

TDD-pins the StorageConnectorProvider Protocol shape + capability enum
+ minimal in-memory fake demonstrating the Protocol is satisfiable.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from axiom.extensions.builtins.connector.storage import (
    FileContent,
    FileRef,
    ListParams,
    PutReceipt,
    ReplyEvent,
    ReplyParams,
    StorageCapability,
    StorageConnectorProvider,
    WatchHandle,
    WatchParams,
)


# ---------------------------------------------------------------------------
# Capability enum
# ---------------------------------------------------------------------------


def test_capability_enum_has_four_capabilities_plus_list():
    """ADR-062 §Decision pins 4 capabilities (read/write/watch/reply
    ingest); LIST is the implicit fifth (listing is the universal
    pre-step for any other capability)."""
    names = {c.name for c in StorageCapability}
    assert names == {"LIST", "READ", "WRITE", "WATCH", "REPLY_INGEST"}


# ---------------------------------------------------------------------------
# In-memory fake — proves Protocol is satisfiable without subclassing
# ---------------------------------------------------------------------------


class _InMemoryStorage:
    """Minimal fake satisfying StorageConnectorProvider.

    Does not subclass the Protocol; relies on structural typing per
    @runtime_checkable. If the Protocol grows a method the fake doesn't
    implement, the isinstance check below catches it.
    """

    vendor = "memfs"
    capabilities = frozenset({
        StorageCapability.LIST,
        StorageCapability.READ,
        StorageCapability.WRITE,
    })

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def list_files(self, params: ListParams) -> Iterator[FileRef]:
        for path in sorted(self._store):
            if path.startswith(params.folder):
                yield FileRef(vendor=self.vendor, path=path)

    def get_file(self, ref: FileRef) -> FileContent:
        return FileContent(data=self._store[ref.path])

    def put_file(self, ref: FileRef, content: FileContent) -> PutReceipt:
        assert content.data is not None
        self._store[ref.path] = content.data
        return PutReceipt(ref=ref, bytes_written=len(content.data))

    def start_watch(self, params: WatchParams) -> WatchHandle:
        # Capability not in the set; memfs raises if called.
        raise NotImplementedError("memfs does not support watch")

    def ingest_replies(self, params: ReplyParams) -> Iterator[ReplyEvent]:
        raise NotImplementedError("memfs does not support reply ingest")


def test_fake_satisfies_storage_connector_protocol():
    fake = _InMemoryStorage()
    assert isinstance(fake, StorageConnectorProvider)


def test_fake_write_then_read_round_trip():
    fake = _InMemoryStorage()
    ref = FileRef(vendor="memfs", path="/run/log.txt")
    receipt = fake.put_file(ref, FileContent(data=b"hello"))
    assert receipt.bytes_written == 5
    assert receipt.ref == ref
    assert fake.get_file(ref).data == b"hello"


def test_fake_list_files_filters_by_folder():
    fake = _InMemoryStorage()
    fake.put_file(FileRef("memfs", "/a/1"), FileContent(data=b"x"))
    fake.put_file(FileRef("memfs", "/b/2"), FileContent(data=b"y"))
    fake.put_file(FileRef("memfs", "/a/3"), FileContent(data=b"z"))
    refs = list(fake.list_files(ListParams(folder="/a")))
    assert [r.path for r in refs] == ["/a/1", "/a/3"]


def test_absent_capability_means_method_raises_not_implemented():
    """Vendors signal absence via the capabilities set; calling an
    absent method is allowed to raise. This pins the contract: callers
    that consult `capabilities` first never hit the raise."""
    fake = _InMemoryStorage()
    assert StorageCapability.WATCH not in fake.capabilities
    with pytest.raises(NotImplementedError):
        fake.start_watch(WatchParams(folder="/"))


# ---------------------------------------------------------------------------
# Wizard registration — Box appears as a supported vendor
# ---------------------------------------------------------------------------


def test_box_registered_in_connector_wizard():
    """ADR-062 §Wizard registration: ``axi connector add box`` resolves
    to a handler. Per ADR-059 this is the one and only Box entry
    point — no duplicate per-agent wizards."""
    from axiom.extensions.builtins.connector.wizard import (
        get_handler,
        list_vendors,
    )

    assert "box" in list_vendors()
    handler = get_handler("box")
    assert handler.vendor == "box"
    # Box developer-token v1 (OAuth lands with M365 Graph foundation).
    field_keys = {f.key for f in handler.fields}
    assert "developer_token" in field_keys
    assert "folder_id" in field_keys
