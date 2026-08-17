# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the neut signal serve HTTP ingestion server."""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from axiom.extensions.builtins.signals.serve import ROUTE_MAP, create_server


@pytest.fixture
def inbox(tmp_path):
    """Create a temporary inbox directory."""
    inbox_dir = tmp_path / "inbox" / "raw"
    inbox_dir.mkdir(parents=True)
    return inbox_dir


@pytest.fixture
def server(inbox):
    """Start a test server on a random port, yield it, then shut down."""
    srv = create_server(host="127.0.0.1", port=0, inbox_root=inbox)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()


def _url(server, path="/"):
    host, port = server.server_address
    return f"http://{host}:{port}{path}"


class TestStatus:
    """Test GET /status endpoint."""

    def test_empty_inbox(self, server):
        resp = urllib.request.urlopen(_url(server, "/status"))
        data = json.loads(resp.read())
        assert data["counts"] == {}

    def test_counts_files(self, server, inbox):
        # Create some files
        voice_dir = inbox / "voice"
        voice_dir.mkdir()
        (voice_dir / "memo.m4a").write_bytes(b"fake audio")
        (inbox / "note.md").write_text("hello")

        resp = urllib.request.urlopen(_url(server, "/status"))
        data = json.loads(resp.read())
        assert data["counts"]["voice"] == 1
        assert data["counts"]["root"] == 1


class TestUploadPage:
    """Test GET / serves the HTML page."""

    def test_serves_html(self, server):
        resp = urllib.request.urlopen(_url(server, "/"))
        content = resp.read().decode()
        from axiom.infra.branding import get_branding
        assert get_branding().product_name in content
        assert "<html" in content


class TestFileUpload:
    """Test POST /upload file routing."""

    def _upload(self, server, filename, content=b"test data"):
        """Upload a file via multipart POST."""
        _boundary = "----TestBoundary123"
        body = (
            f"------TestBoundary123\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n"
            f"\r\n"
        ).encode() + content + b"\r\n------TestBoundary123--\r\n"

        req = urllib.request.Request(
            _url(server, "/upload"),
            data=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----TestBoundary123",
            },
            method="POST",
        )
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())

    def test_upload_voice_file(self, server, inbox):
        result = self._upload(server, "memo.m4a")
        assert "Saved" in result["message"]
        assert (inbox / "voice" / "memo.m4a").exists()

    def test_upload_markdown_file(self, server, inbox):
        result = self._upload(server, "notes.md", b"# My notes\n")
        assert "Saved" in result["message"]
        assert (inbox / "notes.md").exists()

    def test_upload_vtt_to_teams(self, server, inbox):
        _result = self._upload(server, "meeting.vtt")
        assert (inbox / "teams" / "meeting.vtt").exists()

    def test_upload_unknown_ext_to_other(self, server, inbox):
        _result = self._upload(server, "data.csv")
        assert (inbox / "other" / "data.csv").exists()

    def test_duplicate_filename_gets_timestamp(self, server, inbox):
        self._upload(server, "dup.md", b"first")
        _result = self._upload(server, "dup.md", b"second")
        # Should not overwrite the first
        md_files = list(inbox.glob("dup*.md"))
        assert len(md_files) == 2


class TestNote:
    """Test POST /note endpoint."""

    def test_submit_note(self, server, inbox):
        data = urllib.parse.urlencode({"text": "Remember to check the system logs"}).encode()
        req = urllib.request.Request(
            _url(server, "/note"),
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        resp = urllib.request.urlopen(req)
        result = json.loads(resp.read())
        assert "Note saved" in result["message"]

        # Verify file exists
        notes = list(inbox.glob("note_*.md"))
        assert len(notes) == 1
        content = notes[0].read_text()
        assert "system logs" in content

    def test_empty_note_rejected(self, server):
        data = urllib.parse.urlencode({"text": ""}).encode()
        req = urllib.request.Request(
            _url(server, "/note"),
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            raise AssertionError("Expected error")
        except urllib.error.HTTPError as e:
            assert e.code == 400


class TestRouteMap:
    """Test the extension → subdirectory mapping."""

    def test_voice_extensions(self):
        for ext in (".m4a", ".mp3", ".wav", ".ogg", ".webm"):
            assert ROUTE_MAP[ext] == "voice"

    def test_teams_extensions(self):
        for ext in (".vtt", ".srt"):
            assert ROUTE_MAP[ext] == "teams"

    def test_text_extensions(self):
        for ext in (".md", ".txt"):
            assert ROUTE_MAP[ext] == ""  # root of inbox
