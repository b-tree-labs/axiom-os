# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Off-box backup replication — the ``BackupUploader`` seam + Box impl.

A backup that lives only on the box it protects is half a backup. The
``data.backup`` skill drives this seam when the policy's ``offbox`` leg
is enabled; tests inject a fake uploader, and the Box implementation
below is exercised structurally (request shaping) without live calls.

Box specifics: files at or under 50 MB go through the one-shot
multipart upload (`POST upload.box.com/api/2.0/files/content`); larger
files MUST use the chunked-upload session API (create session → upload
parts with SHA-1 digests → commit). Auth is any of the duck-typed Box
auth objects (CCG / OAuth refresh-token / JWT — all expose
``authorization_header()``), resolved from a SecretRef the same way
``BoxSourceProvider._resolve_jwt_auth`` does.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_log = logging.getLogger("axiom.data_platform.backup_uploader")

_UPLOAD_URL = "https://upload.box.com/api/2.0/files/content"
_SESSION_URL = "https://upload.box.com/api/2.0/files/upload_sessions"

# Box requires the chunked-upload session API for files > 50 MB; we switch
# a little early so the boundary itself never 400s.
CHUNKED_THRESHOLD_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class UploadReceipt:
    """Outcome of one off-box replication attempt."""

    ok: bool
    remote_id: str | None = None
    detail: str = ""


class BackupUploader(Protocol):
    """The seam ``data.backup`` drives for its off-box leg."""

    def upload(self, path: Path, *, folder_id: str) -> UploadReceipt: ...


def resolve_box_auth(secret_ref: str) -> Any:
    """Resolve a SecretRef URL into a duck-typed Box auth object.

    Blob shape selects the flow (same dispatch as the Box source
    provider): OAuth refresh-token → CCG → JWT. All three expose
    ``authorization_header()``.
    """
    from axiom.extensions.builtins.secrets import SecretRef, resolve

    with resolve(SecretRef.parse(secret_ref)) as secret:
        blob = json.loads(secret.as_str())

    from ..sources.box.ccg_auth import BoxCcgAuth, BoxCcgConfig
    from ..sources.box.oauth_auth import BoxOAuthAuth, BoxOAuthConfig

    if BoxOAuthConfig.is_oauth_blob(blob):
        return BoxOAuthAuth(BoxOAuthConfig.from_dict(blob))
    if BoxCcgConfig.is_ccg_blob(blob):
        return BoxCcgAuth(BoxCcgConfig.from_dict(blob))

    from ..sources.box.jwt_auth import BoxJwtAuth, BoxJwtConfig

    return BoxJwtAuth(BoxJwtConfig.from_dict(blob))


class BoxBackupUploader:
    """Box implementation of the :class:`BackupUploader` seam.

    ``auth`` is any object exposing ``authorization_header() -> str``.
    ``session`` is a requests-compatible object (injectable so structural
    tests shape requests without the network).
    """

    def __init__(self, auth: Any, *, session: Any = None) -> None:
        self._auth = auth
        if session is None:  # pragma: no cover — thin import glue
            import requests

            session = requests.Session()
        self._session = session

    # -- public seam --------------------------------------------------------

    def upload(self, path: Path, *, folder_id: str) -> UploadReceipt:
        try:
            size = path.stat().st_size
            if size > CHUNKED_THRESHOLD_BYTES:
                return self._upload_chunked(path, size, folder_id)
            return self._upload_direct(path, folder_id)
        except Exception as exc:  # noqa: BLE001 — the skill reports, HERALD alerts
            _log.warning("box upload failed for %s: %s", path, exc)
            return UploadReceipt(ok=False, detail=f"{type(exc).__name__}: {exc}")

    # -- direct (≤50 MB) ----------------------------------------------------

    def _upload_direct(self, path: Path, folder_id: str) -> UploadReceipt:
        attributes = {"name": path.name, "parent": {"id": str(folder_id)}}
        resp = self._session.post(
            _UPLOAD_URL,
            headers={"Authorization": self._auth.authorization_header()},
            data={"attributes": json.dumps(attributes)},
            files={"file": (path.name, path.read_bytes())},
            timeout=300,
        )
        if resp.status_code not in (200, 201):
            return UploadReceipt(
                ok=False,
                detail=f"direct upload HTTP {resp.status_code}: {_snippet(resp)}",
            )
        return UploadReceipt(ok=True, remote_id=_entry_id(resp), detail="direct upload")

    # -- chunked (>50 MB) ---------------------------------------------------

    def _upload_chunked(self, path: Path, size: int, folder_id: str) -> UploadReceipt:
        headers = {"Authorization": self._auth.authorization_header()}

        # 1. Create the upload session — Box replies with the mandated
        #    part_size and the session id.
        resp = self._session.post(
            _SESSION_URL,
            headers=headers,
            json={"folder_id": str(folder_id), "file_size": size, "file_name": path.name},
            timeout=60,
        )
        if resp.status_code not in (200, 201):
            return UploadReceipt(
                ok=False,
                detail=f"upload-session create HTTP {resp.status_code}: {_snippet(resp)}",
            )
        session_blob = resp.json()
        session_id = session_blob["id"]
        part_size = int(session_blob["part_size"])

        # 2. Upload each part with its SHA-1 digest + Content-Range.
        parts: list[dict[str, Any]] = []
        whole = hashlib.sha1()  # noqa: S324 — Box's mandated digest algorithm
        offset = 0
        with path.open("rb") as fh:
            while offset < size:
                chunk = fh.read(part_size)
                if not chunk:
                    break
                whole.update(chunk)
                part_resp = self._session.put(
                    f"{_SESSION_URL}/{session_id}",
                    headers={
                        **headers,
                        "Digest": _sha1_digest(chunk),
                        "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}",
                        "Content-Type": "application/octet-stream",
                    },
                    data=chunk,
                    timeout=300,
                )
                if part_resp.status_code not in (200, 201):
                    return UploadReceipt(
                        ok=False,
                        detail=(
                            f"part upload at offset {offset} HTTP "
                            f"{part_resp.status_code}: {_snippet(part_resp)}"
                        ),
                    )
                parts.append(part_resp.json()["part"])
                offset += len(chunk)

        # 3. Commit with the whole-file digest.
        commit_resp = self._session.post(
            f"{_SESSION_URL}/{session_id}/commit",
            headers={
                **headers,
                "Digest": f"sha={base64.b64encode(whole.digest()).decode()}",
            },
            json={"parts": parts},
            timeout=300,
        )
        if commit_resp.status_code not in (200, 201, 202):
            return UploadReceipt(
                ok=False,
                detail=f"commit HTTP {commit_resp.status_code}: {_snippet(commit_resp)}",
            )
        return UploadReceipt(
            ok=True,
            remote_id=_entry_id(commit_resp),
            detail=f"chunked upload ({len(parts)} parts)",
        )


def _sha1_digest(chunk: bytes) -> str:
    return "sha=" + base64.b64encode(hashlib.sha1(chunk).digest()).decode()  # noqa: S324


def _entry_id(resp: Any) -> str | None:
    try:
        entries = resp.json().get("entries") or []
        return str(entries[0]["id"]) if entries else None
    except Exception:  # noqa: BLE001 — receipt id is best-effort metadata
        return None


def _snippet(resp: Any) -> str:
    try:
        return str(resp.text)[:200]
    except Exception:  # noqa: BLE001
        return "<no body>"


__all__ = [
    "CHUNKED_THRESHOLD_BYTES",
    "BackupUploader",
    "BoxBackupUploader",
    "UploadReceipt",
    "resolve_box_auth",
]
