# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the coordinator's static landing page (D1).

This is the first surface anyone hits in a browser — instructors
forward the URL to prospective students; it needs to be explicit
about what the class is, how to join, and how to get help on the
CLI once joined.
"""

from __future__ import annotations

import threading
import urllib.request
from http.server import HTTPServer

import pytest

from axiom.extensions.builtins.classroom.classroom_federation import (
    create_cohort,
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
from axiom.vega.federation.identity import generate_identity


@pytest.fixture
def running_server(tmp_path):
    coord = generate_identity(owner="prof@ut.edu", keys_dir=tmp_path / "keys")
    coord_dir = tmp_path / "coord"
    cohort_store = FileCohortStore(coord_dir)
    cohort_store.save(
        create_cohort("NE_PRAGUE_2026", coord.node_id),
        coordinator_url="http://placeholder/classroom/join",
    )
    invite_registry = FileInviteRegistry(coord_dir / "invites.json")
    handler_cls = make_coordinator_handler(
        coordinator_identity=coord,
        classroom_id="NE_PRAGUE_2026",
        cohort_store=cohort_store,
        invite_registry=invite_registry,
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type"), resp.read()


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------


class TestLanding:
    def test_root_serves_html(self, running_server):
        status, ct, body = _get(running_server + "/")
        assert status == 200
        assert ct.startswith("text/html")
        assert b"<!DOCTYPE html>" in body

    def test_landing_substitutes_classroom_id(self, running_server):
        status, _, body = _get(running_server + "/")
        assert status == 200
        text = body.decode("utf-8")
        # Real classroom id is interpolated; no stray template tokens remain.
        assert "NE_PRAGUE_2026" in text
        assert "{{ CLASSROOM_ID }}" not in text

    def test_landing_shows_join_cli_snippet(self, running_server):
        _, _, body = _get(running_server + "/")
        text = body.decode("utf-8")
        # The single critical CLI invocation students need.
        assert "axi classroom join" in text

    def test_landing_covers_both_roles(self, running_server):
        _, _, body = _get(running_server + "/")
        text = body.decode("utf-8").lower()
        assert "student" in text
        assert "instructor" in text

    def test_index_html_aliases_root(self, running_server):
        a = _get(running_server + "/")[2]
        b = _get(running_server + "/index.html")[2]
        assert a == b


# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------


class TestStyleAsset:
    def test_css_served_with_correct_content_type(self, running_server):
        status, ct, body = _get(running_server + "/webui/style.css")
        assert status == 200
        assert ct.startswith("text/css")
        assert b"body {" in body or b"main {" in body

    def test_traversal_attempt_blocked(self, running_server):
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(running_server + "/webui/../cli.py")
        assert exc_info.value.code == 404

    def test_unknown_asset_404(self, running_server):
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(running_server + "/webui/nothing.png")
        assert exc_info.value.code == 404


# ---------------------------------------------------------------------------
# Mobile-friendliness smoke (just check viewport meta tag is present)
# ---------------------------------------------------------------------------


class TestMobileFriendly:
    def test_viewport_meta_tag_present(self, running_server):
        _, _, body = _get(running_server + "/")
        text = body.decode("utf-8")
        assert "viewport" in text
        assert "device-width" in text
