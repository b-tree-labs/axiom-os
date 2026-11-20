# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the coordinator's materials HTTP endpoints.

Phase 2 of the materials-flow tier. The instructor's `axi classroom
serve` process exposes two read-only endpoints on top of the existing
join POST:

    GET  /classroom/materials/manifest        → signed JSON manifest
    GET  /classroom/materials/<file_id>        → raw file bytes

Together they let a joining student pull everything they need for
local indexing — no shared filesystem, no scp, no instructor
intervention per-student.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from axiom.extensions.builtins.classroom.classroom_federation import (
    create_cohort,
)
from axiom.extensions.builtins.classroom.classroom_materials import (
    ClassroomMaterialsStore,
    compute_file_id,
)
from axiom.extensions.builtins.classroom.coordinator_cohort_store import (
    FileCohortStore,
)
from axiom.extensions.builtins.classroom.coordinator_invite_registry import (
    FileInviteRegistry,
)
from axiom.extensions.builtins.classroom.coordinator_server import (
    make_coordinator_handler,
)
from axiom.extensions.builtins.classroom.materials_manifest import (
    decode_materials_manifest,
    verify_materials_manifest,
)
from axiom.vega.federation.identity import generate_identity


@pytest.fixture
def coordinator_fixture(tmp_path):
    coord = generate_identity(
        owner="prof@ut.edu", keys_dir=tmp_path / "coord-keys"
    )
    coord_dir = tmp_path / "coord"
    classroom_id = "NE101"

    cohort = create_cohort(classroom_id, coord.node_id)
    cohort_store = FileCohortStore(coord_dir)
    cohort_store.save(cohort, coordinator_url="http://placeholder/classroom/join")
    registry = FileInviteRegistry(coord_dir / "invites.json")

    materials = ClassroomMaterialsStore(
        coord_dir / "classrooms" / classroom_id
    )
    e1 = materials.add_text(
        "Fission splits heavy nuclei into lighter fragments.",
        filename="ch1.md",
        title="Chapter 1 — Fission",
    )
    e2 = materials.add_text(
        "Fusion combines light nuclei into heavier ones.",
        filename="ch2.md",
    )

    handler_cls = make_coordinator_handler(
        coordinator_identity=coord,
        classroom_id=classroom_id,
        cohort_store=cohort_store,
        invite_registry=registry,
        materials_store=materials,
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "coord": coord,
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "entries": [e1, e2],
            "materials": materials,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""


# ---------------------------------------------------------------------------
# Manifest endpoint
# ---------------------------------------------------------------------------


class TestManifestEndpoint:
    def test_manifest_served_as_json(self, coordinator_fixture):
        status, body = _get(
            coordinator_fixture["base_url"] + "/classroom/materials/manifest"
        )
        assert status == 200
        manifest = decode_materials_manifest(body.decode("utf-8"))
        assert manifest.classroom_id == "NE101"
        assert len(manifest.entries) == 2

    def test_manifest_signature_verifies_against_coordinator_key(
        self, coordinator_fixture,
    ):
        status, body = _get(
            coordinator_fixture["base_url"] + "/classroom/materials/manifest"
        )
        assert status == 200
        manifest = decode_materials_manifest(body.decode("utf-8"))
        result = verify_materials_manifest(
            manifest,
            coordinator_public_key=coordinator_fixture["coord"].public_key,
        )
        assert result.valid is True


# ---------------------------------------------------------------------------
# File endpoint
# ---------------------------------------------------------------------------


class TestFileEndpoint:
    def test_known_file_id_returns_content(self, coordinator_fixture):
        entry = coordinator_fixture["entries"][0]
        status, body = _get(
            coordinator_fixture["base_url"]
            + f"/classroom/materials/{entry.file_id}"
        )
        assert status == 200
        assert body == (
            b"Fission splits heavy nuclei into lighter fragments."
        )

    def test_content_matches_manifest_hash(self, coordinator_fixture):
        entry = coordinator_fixture["entries"][0]
        status, body = _get(
            coordinator_fixture["base_url"]
            + f"/classroom/materials/{entry.file_id}"
        )
        assert status == 200
        # The file_id IS the content hash; the student will re-verify
        # after download.
        assert compute_file_id(body) == entry.file_id

    def test_unknown_file_id_returns_404(self, coordinator_fixture):
        status, body = _get(
            coordinator_fixture["base_url"]
            + "/classroom/materials/not-a-real-file-id"
        )
        assert status == 404


# ---------------------------------------------------------------------------
# Back-compat — legacy server without materials still serves join
# ---------------------------------------------------------------------------


class TestBackwardsCompat:
    def test_server_without_materials_store_still_works(self, tmp_path):
        """A coordinator built without the new materials argument must
        still handle join POSTs (that was the whole server before this
        PR). This locks in the opt-in nature of materials serving."""
        coord = generate_identity(
            owner="prof@ut.edu", keys_dir=tmp_path / "keys"
        )
        cohort_store = FileCohortStore(tmp_path / "coord")
        cohort_store.save(
            create_cohort("NE101", coord.node_id),
            coordinator_url="http://x/classroom/join",
        )
        registry = FileInviteRegistry(tmp_path / "coord" / "invites.json")

        handler_cls = make_coordinator_handler(
            coordinator_identity=coord,
            classroom_id="NE101",
            cohort_store=cohort_store,
            invite_registry=registry,
            # materials_store deliberately omitted
        )
        server = HTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            # Materials endpoints should 404 gracefully, not crash.
            status, _ = _get(url + "/classroom/materials/manifest")
            assert status == 404
            status, _ = _get(url + "/classroom/materials/anything")
            assert status == 404
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
