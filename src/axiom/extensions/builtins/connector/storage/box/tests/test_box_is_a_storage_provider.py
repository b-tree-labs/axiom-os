# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Box is reached as a storage provider, by connector name, through the registry.

End to end on a loopback fake of the Box API: a connector FILE (the one the
platform already keeps), the registry's discovery, the manager's mount and
probe, the provider's own auth resolution (client credentials from a secret
reference), the REST client's pagination and the content download. Nothing in
the test asks for the vendor except the connector's ``kind``.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.connector.storage import registry
from axiom.extensions.builtins.connector.storage.manager import (
    ConnectorHealth,
    StorageConnectorManager,
    connector_file_specs,
)
from axiom.extensions.builtins.connector.storage.protocol import (
    FileRef,
    ListParams,
    StorageCapability,
    StorageConnectorProvider,
)
from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
    ConnectorConfig,
    save_connector,
)
from axiom.extensions.builtins.data_platform.sources.box.tests.fake_box import (
    FakeBox,
    FakeBoxServer,
)


@pytest.fixture
def box(monkeypatch):
    world = FakeBox()
    world.add_folder("1001", "Drops")
    world.add_folder("1002", "Run 7", parent="1001", shared=False)
    world.add_file("2001", "run-0007.csv", parent="1002", size=2048)
    world.add_file("2003", "notes.txt", parent="1001", size=10)
    world.add_folder("1003", "Not shared", shared=False)
    with FakeBoxServer(world) as server:
        # The OLD import paths: a patch there must reach the moved modules.
        from axiom.extensions.builtins.data_platform.sources.box import ccg_auth, session_api

        monkeypatch.setattr(ccg_auth, "_TOKEN_URL", server.url + "/oauth2/token")
        monkeypatch.setattr(session_api, "_BOX_API_BASE", server.url + "/2.0")
        for var in (
            "HTTPS_PROXY",
            "https_proxy",
            "HTTP_PROXY",
            "http_proxy",
            "ALL_PROXY",
            "all_proxy",
            "BOX_DEVELOPER_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv(
            "TEAM_SHARE_APP",
            json.dumps(
                {
                    "client_id": world.client_id,
                    "client_secret": world.client_secret,
                    "enterprise_id": world.enterprise_id,
                }
            ),
        )
        yield world


def _declare(tmp_path, *, folder: str = "1001", ref: str = "env://TEAM_SHARE_APP") -> None:
    save_connector(
        ConnectorConfig(
            name="team-share",
            kind="box",
            bronze_root=str(tmp_path / "bronze"),
            params={
                "folder_id": folder,
                "jwt_secret_ref": ref,
                "session_dir": str(tmp_path / "session"),
            },
        ),
        state_dir=tmp_path,
    )


def _manager(tmp_path) -> StorageConnectorManager:
    return StorageConnectorManager(load_specs=lambda: connector_file_specs(tmp_path))


def test_the_registry_offers_the_vendor_without_core_code_naming_it():
    assert {"box", "local"} <= set(registry.available_storage_connectors())


def test_a_box_connector_file_mounts_as_a_storage_provider(box, tmp_path):
    _declare(tmp_path)
    m = _manager(tmp_path)
    status = m.reload()["team-share"]
    assert status.health is ConnectorHealth.READY, status.reason
    assert status.kind == "box" and "Drops" in status.reason
    with m.lease("team-share") as store:
        assert isinstance(store, StorageConnectorProvider)
        assert StorageCapability.LIST in store.capabilities
        assert StorageCapability.WRITE not in store.capabilities  # phase 2


def test_listing_walks_the_folder_and_carries_what_box_states(box, tmp_path):
    _declare(tmp_path)
    with _manager(tmp_path).lease("team-share") as store:
        top = list(store.list_files(ListParams(folder="")))
        every = list(store.list_files(ListParams(folder="", recursive=True)))
    assert [r.hints["name"] for r in top] == ["notes.txt"]
    assert sorted(r.hints["name"] for r in every) == ["notes.txt", "run-0007.csv"]
    run = next(r for r in every if r.hints["name"] == "run-0007.csv")
    assert run.vendor == "box" and run.path == "2001"
    assert run.hints["folder_path"] == "/All Files/Drops/Run 7"
    assert run.hints["etag"] == "1" and run.hints["modified_at"]


def test_a_listed_file_is_read_through_the_same_provider(box, tmp_path):
    _declare(tmp_path)
    with _manager(tmp_path).lease("team-share") as store:
        content = store.get_file(FileRef(vendor="box", path="2001"))
    assert content.data == b"bytes of run-0007.csv"


def test_a_folder_the_app_cannot_see_fails_its_probe_by_name(box, tmp_path):
    _declare(tmp_path, folder="1003")
    status = _manager(tmp_path).reload()["team-share"]
    assert status.health is ConnectorHealth.FAILED
    assert "404" in status.reason


def test_a_wrong_credential_fails_the_probe_and_never_serves(box, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TEAM_SHARE_APP",
        json.dumps(
            {
                "client_id": box.client_id,
                "client_secret": "wrong",
                "enterprise_id": box.enterprise_id,
            }
        ),
    )
    _declare(tmp_path)
    status = _manager(tmp_path).reload()["team-share"]
    assert status.health is ConnectorHealth.FAILED


def test_write_is_refused_until_its_phase(box, tmp_path):
    _declare(tmp_path)
    with _manager(tmp_path).lease("team-share") as store:
        with pytest.raises(NotImplementedError, match="phase 2"):
            store.put_file(FileRef(vendor="box", path="x"), None)


def test_the_old_import_paths_are_the_moved_modules():
    from axiom.extensions.builtins.connector.storage.box import ccg_auth as new
    from axiom.extensions.builtins.data_platform.sources.box import ccg_auth as old

    assert old is new
