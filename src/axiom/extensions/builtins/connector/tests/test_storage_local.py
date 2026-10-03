# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LocalStorageConnector — the first StorageConnectorProvider implementation
(ADR-062 / connector-cohesion follow-on). A directory-backed file-store proving
the four-capability seam works end to end, no cloud: list / read / write. The
storage analogue of P4's LocalFileEditor for the editor seam."""

from __future__ import annotations

from axiom.extensions.builtins.connector.storage import (
    FileContent,
    FileRef,
    ListParams,
    StorageCapability,
    StorageConnectorProvider,
)
from axiom.extensions.builtins.connector.storage.local import LocalStorageConnector
from axiom.extensions.builtins.connector.storage.registry import (
    available_storage_connectors,
    get_storage_connector,
    register_storage_connector,
)


def test_satisfies_the_protocol_and_declares_capabilities(tmp_path):
    conn = LocalStorageConnector(root=str(tmp_path))
    assert isinstance(conn, StorageConnectorProvider)
    assert conn.vendor == "local"
    assert {StorageCapability.LIST, StorageCapability.READ,
            StorageCapability.WRITE} <= conn.capabilities


def test_put_get_list_round_trip(tmp_path):
    conn = LocalStorageConnector(root=str(tmp_path))
    receipt = conn.put_file(FileRef(vendor="local", path="docs/a.txt"),
                            FileContent(data=b"hello\n", media_type="text/plain"))
    assert receipt.bytes_written == 6
    assert (tmp_path / "docs" / "a.txt").read_bytes() == b"hello\n"

    got = conn.get_file(FileRef(vendor="local", path="docs/a.txt"))
    assert got.data == b"hello\n"

    conn.put_file(FileRef(vendor="local", path="docs/b.txt"), FileContent(data=b"two\n"))
    listed = sorted(r.path for r in conn.list_files(ListParams(folder="docs")))
    assert listed == ["docs/a.txt", "docs/b.txt"]
    assert all(r.vendor == "local" for r in conn.list_files(ListParams(folder="docs")))


def test_recursive_list(tmp_path):
    conn = LocalStorageConnector(root=str(tmp_path))
    conn.put_file(FileRef(vendor="local", path="x/y/deep.txt"), FileContent(data=b"z"))
    conn.put_file(FileRef(vendor="local", path="x/top.txt"), FileContent(data=b"t"))
    top = sorted(r.path for r in conn.list_files(ListParams(folder="x")))
    assert top == ["x/top.txt"]                       # non-recursive: top level only
    deep = sorted(r.path for r in conn.list_files(ListParams(folder="x", recursive=True)))
    assert deep == ["x/top.txt", "x/y/deep.txt"]


def test_registered_and_resolvable_as_a_connector_kind(tmp_path):
    assert "local" in available_storage_connectors()
    conn = get_storage_connector("local", root=str(tmp_path))
    assert isinstance(conn, LocalStorageConnector)


def test_a_new_vendor_is_one_register_call():
    register_storage_connector("fake-store", lambda **c: ("FAKE", c), replace=True)
    try:
        assert "fake-store" in available_storage_connectors()
        assert get_storage_connector("fake-store", root="/x") == ("FAKE", {"root": "/x"})
    finally:
        from axiom.extensions.builtins.connector.storage import registry
        registry._STORES.unregister("fake-store")
