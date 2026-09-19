# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Structural tests for the Box backup uploader (request shaping only).

No live Box calls: a fake requests-session records every request and
returns canned responses, pinning the wire shape of both the one-shot
multipart upload (≤50 MB) and the chunked-upload session flow (>50 MB:
create session → parts with SHA-1 digests + Content-Range → commit).
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from axiom.extensions.builtins.data_platform.database import backup_uploader as bu
from axiom.extensions.builtins.data_platform.database.backup_uploader import (
    BoxBackupUploader,
)


class _FakeAuth:
    def authorization_header(self) -> str:
        return "Bearer test-token"


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    """Records (method, url, kwargs); pops canned responses in order."""

    def __init__(self, responses: list[_FakeResponse]):
        self.calls: list[tuple[str, str, dict]] = []
        self._responses = list(responses)

    def _next(self) -> _FakeResponse:
        return self._responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._next()

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return self._next()


def _artifact(tmp_path: Path, size: int) -> Path:
    p = tmp_path / "axiom-backup-20260711_020000.dump"
    p.write_bytes(b"x" * size)
    return p


class TestDirectUpload:
    def test_direct_request_shape(self, tmp_path):
        session = _FakeSession([_FakeResponse(201, {"entries": [{"id": "f-42"}]})])
        up = BoxBackupUploader(_FakeAuth(), session=session)
        artifact = _artifact(tmp_path, 128)

        receipt = up.upload(artifact, folder_id="777")

        assert receipt.ok
        assert receipt.remote_id == "f-42"
        method, url, kwargs = session.calls[0]
        assert (method, url) == ("POST", "https://upload.box.com/api/2.0/files/content")
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"
        attributes = json.loads(kwargs["data"]["attributes"])
        assert attributes["name"] == artifact.name
        assert attributes["parent"] == {"id": "777"}
        fname, fbytes = kwargs["files"]["file"]
        assert fname == artifact.name
        assert fbytes == b"x" * 128

    def test_direct_failure_reported(self, tmp_path):
        session = _FakeSession([_FakeResponse(409, {"message": "name in use"})])
        up = BoxBackupUploader(_FakeAuth(), session=session)
        receipt = up.upload(_artifact(tmp_path, 8), folder_id="777")
        assert not receipt.ok
        assert "409" in receipt.detail


class TestChunkedUpload:
    def test_chunked_flow_shape(self, tmp_path, monkeypatch):
        # Shrink the threshold so the test file exercises the chunked path.
        monkeypatch.setattr(bu, "CHUNKED_THRESHOLD_BYTES", 10)
        content_size = 25  # → 3 parts at part_size=10
        artifact = _artifact(tmp_path, content_size)
        data = artifact.read_bytes()

        session = _FakeSession(
            [
                _FakeResponse(201, {"id": "sess-1", "part_size": 10}),
                _FakeResponse(200, {"part": {"part_id": "p1", "offset": 0, "size": 10}}),
                _FakeResponse(200, {"part": {"part_id": "p2", "offset": 10, "size": 10}}),
                _FakeResponse(200, {"part": {"part_id": "p3", "offset": 20, "size": 5}}),
                _FakeResponse(201, {"entries": [{"id": "f-99"}]}),
            ]
        )
        up = BoxBackupUploader(_FakeAuth(), session=session)

        receipt = up.upload(artifact, folder_id="777")

        assert receipt.ok
        assert receipt.remote_id == "f-99"
        assert "3 parts" in receipt.detail

        # 1. Session creation.
        method, url, kwargs = session.calls[0]
        assert (method, url) == (
            "POST",
            "https://upload.box.com/api/2.0/files/upload_sessions",
        )
        assert kwargs["json"] == {
            "folder_id": "777",
            "file_size": content_size,
            "file_name": artifact.name,
        }

        # 2. Parts: Content-Range + per-part SHA-1 digest.
        part_calls = session.calls[1:4]
        expected_ranges = [
            f"bytes 0-9/{content_size}",
            f"bytes 10-19/{content_size}",
            f"bytes 20-24/{content_size}",
        ]
        for (method, url, kwargs), expected_range, lo, hi in zip(
            part_calls, expected_ranges, (0, 10, 20), (10, 20, 25)
        ):
            assert method == "PUT"
            assert url.endswith("/upload_sessions/sess-1")
            assert kwargs["headers"]["Content-Range"] == expected_range
            expected_digest = "sha=" + base64.b64encode(hashlib.sha1(data[lo:hi]).digest()).decode()
            assert kwargs["headers"]["Digest"] == expected_digest
            assert kwargs["data"] == data[lo:hi]

        # 3. Commit: ordered parts + whole-file digest.
        method, url, kwargs = session.calls[4]
        assert method == "POST"
        assert url.endswith("/upload_sessions/sess-1/commit")
        assert [p["part_id"] for p in kwargs["json"]["parts"]] == ["p1", "p2", "p3"]
        whole = "sha=" + base64.b64encode(hashlib.sha1(data).digest()).decode()
        assert kwargs["headers"]["Digest"] == whole

    def test_part_failure_aborts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bu, "CHUNKED_THRESHOLD_BYTES", 10)
        session = _FakeSession(
            [
                _FakeResponse(201, {"id": "sess-1", "part_size": 10}),
                _FakeResponse(200, {"part": {"part_id": "p1", "offset": 0, "size": 10}}),
                _FakeResponse(500, {"message": "hiccup"}),
            ]
        )
        up = BoxBackupUploader(_FakeAuth(), session=session)
        receipt = up.upload(_artifact(tmp_path, 25), folder_id="777")
        assert not receipt.ok
        assert "offset 10" in receipt.detail
        assert len(session.calls) == 3  # no further parts, no commit

    def test_session_create_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bu, "CHUNKED_THRESHOLD_BYTES", 10)
        session = _FakeSession([_FakeResponse(403, {"message": "no"})])
        up = BoxBackupUploader(_FakeAuth(), session=session)
        receipt = up.upload(_artifact(tmp_path, 25), folder_id="777")
        assert not receipt.ok
        assert "403" in receipt.detail


class TestExceptionBoundary:
    def test_unexpected_exception_becomes_receipt(self, tmp_path):
        class _ExplodingSession:
            def post(self, *a, **kw):
                raise ConnectionError("network down")

        up = BoxBackupUploader(_FakeAuth(), session=_ExplodingSession())
        receipt = up.upload(_artifact(tmp_path, 8), folder_id="777")
        assert not receipt.ok
        assert "ConnectionError" in receipt.detail
