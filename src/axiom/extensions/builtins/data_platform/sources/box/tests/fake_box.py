# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An in-process fake of the slice of the Box API a connector setup touches.

A real HTTP server on a loopback port, so the code under test runs its own
``requests`` calls, token exchange and pagination exactly as it would against
Box. It answers with Box's own error shapes: ``invalid_client`` for a wrong id or
secret, ``invalid_grant`` for an enterprise the app is not in,
``unauthorized_client`` for an app no admin authorized, and ``404 not_found`` for
a folder not shared with the app's service account.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


@dataclass
class FakeBox:
    client_id: str = "cid-0123456789"
    client_secret: str = "s3cr3t-value-never-on-disk"
    enterprise_id: str = "424242"
    authorized: bool = True
    login: str = "AutomationUser_1_abc@boxdevedition.com"
    folders: dict[str, dict] = field(default_factory=dict)  # id -> {name, parent, shared}
    files: dict[str, dict] = field(default_factory=dict)  # id -> {name, parent, size, sha1}
    requests: list[str] = field(default_factory=list)
    _tokens: set[str] = field(default_factory=set)

    # -- building the world ------------------------------------------------

    def add_folder(self, fid: str, name: str, parent: str = "0", shared: bool = True) -> None:
        self.folders[fid] = {"name": name, "parent": parent, "shared": shared}

    def add_file(self, fid: str, name: str, parent: str, size: int = 1024) -> None:
        self.files[fid] = {"name": name, "parent": parent, "size": size, "sha1": f"{int(fid):040x}"}

    # -- visibility --------------------------------------------------------

    def _visible(self, fid: str) -> bool:
        """A folder the service account sees: shared with it, or inside one that is."""
        while fid in self.folders:
            if self.folders[fid]["shared"]:
                return True
            fid = self.folders[fid]["parent"]
        return False

    def _path(self, parent: str) -> list[dict]:
        chain = []
        while parent in self.folders:
            chain.append({"id": parent, "name": self.folders[parent]["name"]})
            parent = self.folders[parent]["parent"]
        return [{"id": "0", "name": "All Files"}, *reversed(chain)]

    def _children(self, fid: str) -> list[dict]:
        out = []
        for cid, f in self.folders.items():
            if f["parent"] == fid and (fid != "0" or f["shared"]):
                out.append({"type": "folder", "id": cid, "name": f["name"], "etag": "0",
                            "path_collection": {"entries": self._path(fid)}})
        for cid, f in self.files.items():
            if f["parent"] == fid:
                out.append({"type": "file", "id": cid, "name": f["name"], "etag": "1",
                            "size": f["size"], "modified_at": "2026-10-20T12:00:00-05:00",
                            "path_collection": {"entries": self._path(fid)}})
        return out

    # -- serving -----------------------------------------------------------

    def handle(self, method: str, path: str, query: dict, body: str, auth: str) -> tuple[int, dict]:
        self.requests.append(f"{method} {path}")
        if method == "POST" and path == "/oauth2/token":
            form = {k: v[0] for k, v in parse_qs(body).items()}
            if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
                return 400, {"error": "invalid_client", "error_description": "The client credentials are invalid"}
            if not self.authorized:
                return 400, {"error": "unauthorized_client",
                             "error_description": "The \"box_subject_type\" value is unauthorized for this client_id"}
            if form.get("box_subject_id") != self.enterprise_id:
                return 400, {"error": "invalid_grant", "error_description": "Grant credentials are invalid"}
            token = f"tok-{len(self._tokens) + 1}"
            self._tokens.add(token)
            return 200, {"access_token": token, "expires_in": 3600, "token_type": "bearer"}

        if auth.removeprefix("Bearer ") not in self._tokens:
            return 401, {"type": "error", "status": 401, "code": "unauthorized"}
        if not path.startswith("/2.0/"):
            return 404, {"type": "error", "status": 404, "code": "not_found"}
        parts = path[len("/2.0/"):].strip("/").split("/")
        if parts == ["users", "me"]:
            return 200, {"type": "user", "login": self.login, "name": "Service Account"}
        not_found = (404, {"type": "error", "status": 404, "code": "not_found", "message": "Not Found"})
        if parts[0] == "folders" and len(parts) in (2, 3):
            fid = parts[1]
            if fid != "0" and not self._visible(fid):
                return not_found
            children = self._children(fid)
            if len(parts) == 3 and parts[2] == "items":
                return 200, {"entries": children, "total_count": len(children)}
            name = "All Files" if fid == "0" else self.folders[fid]["name"]
            return 200, {"type": "folder", "id": fid, "name": name,
                         "item_collection": {"total_count": len(children)}}
        if parts[0] == "files" and len(parts) == 2:
            f = self.files.get(parts[1])
            if f is None or not self._visible(f["parent"]):
                return not_found
            return 200, {"type": "file", "id": parts[1], "name": f["name"], "size": f["size"],
                         "sha1": f["sha1"], "modified_at": "2026-10-20T12:00:00-05:00"}
        return not_found


class FakeBoxServer:
    """Serve a :class:`FakeBox` on 127.0.0.1 in a daemon thread."""

    def __init__(self, box: FakeBox) -> None:
        self.box = box
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _go(self, method: str) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                status, payload = outer.box.handle(method, parts.path, parse_qs(parts.query), body,
                                                   self.headers.get("Authorization") or "")
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
                self._go("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._go("POST")

            def log_message(self, *_args) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02},
                                        daemon=True)

    def __enter__(self) -> FakeBoxServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()
