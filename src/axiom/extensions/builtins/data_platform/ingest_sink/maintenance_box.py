# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The edge's maintenance mailbox (ADR-183).

The relay between an operator and a site's node. It holds signed requests
addressed to a site until that site's node fetches them over its own outbound
connection, and holds the results the node posts back until an operator reads
them. It never runs anything and never needs to trust a request: the node
verifies every signature itself against keys it was given at install.

What the relay does enforce is who may talk to it:

* a node reads only its own site's requests and posts only its own results,
  the site taken from its credential (ADR-106 tenancy), never from the payload;
* only principals listed in ``AXIOM_MAINTENANCE_OPERATORS`` may post requests
  or read results, and a listed principal is never treated as a site's node.

Storage is one append-only JSONL file per site for requests and one for results.

It also relays support sessions (level 2). A person at the site opens one with
``support open``; their node creates it here over its outbound connection and
then carries a terminal's bytes both ways by plain HTTPS long-polling, which
needs no WebSocket upgrade from the proxy in front of this edge or from any
proxy at the site. The relay records both directions (asciicast v2, one file
per session), refuses anything after the session's end, and lets either side
close it. Only the site's own node can open a session: no operator call can.
"""

# No ``from __future__ import annotations``: the route handlers below are
# defined inside the builder, with FastAPI's types imported there, and FastAPI
# resolves string annotations against module globals, where those names are not.
import asyncio
import base64
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

OPERATORS_ENV = "AXIOM_MAINTENANCE_OPERATORS"
DIR_ENV = "AXIOM_MAINTENANCE_DIR"
MAX_PAGE = 100
_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class MaintenanceBox:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "MaintenanceBox | None":
        root = os.environ.get(DIR_ENV)
        if not root:
            outbox = os.environ.get("AXIOM_INGEST_OUTBOX_DIR")
            if not outbox:
                return None
            root = str(Path(outbox).parent / "maintenance")
        return cls(root)

    def _file(self, site: str, kind: str) -> Path:
        if not _SITE.fullmatch(site):
            raise ValueError(f"not a site name: {site!r}")
        return self.root / site / f"{kind}.jsonl"

    def _append(self, site: str, kind: str, record: dict[str, Any]) -> int:
        path = self._file(site, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            seq = sum(1 for _ in path.open(encoding="utf-8")) + 1 if path.exists() else 1
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"seq": seq, **record}, sort_keys=True) + "\n")
        return seq

    def _read(self, site: str, kind: str, after: int, limit: int) -> list[dict[str, Any]]:
        path = self._file(site, kind)
        if not path.exists():
            return []
        out = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if rec["seq"] > after:
                    out.append(rec)
                    if len(out) >= limit:
                        break
        return out

    def add_request(self, envelope: dict[str, Any], *, by: str) -> int:
        site = str((envelope.get("request") or {}).get("site") or "")
        return self._append(site, "requests", {"envelope": envelope, "posted_by": by})

    def requests(self, site: str, *, after: int = 0, limit: int = MAX_PAGE) -> list[dict[str, Any]]:
        return self._read(site, "requests", after, limit)

    def add_results(self, site: str, results: list[dict[str, Any]]) -> int:
        seq = 0
        for result in results:
            seq = self._append(site, "results", {"result": result})
        return seq

    def results(self, site: str, *, after: int = 0, limit: int = MAX_PAGE) -> list[dict[str, Any]]:
        return self._read(site, "results", after, limit)

    def find_request(self, site: str, request_id: str) -> dict[str, Any] | None:
        """The request envelope with this id in a site's mailbox, for showing what a link decides."""
        after = 0
        while True:
            page = self._read(site, "requests", after, MAX_PAGE)
            if not page:
                return None
            for rec in page:
                env = rec.get("envelope") or {}
                if str((env.get("request") or {}).get("id")) == request_id:
                    return env
            after = page[-1]["seq"]

    def add_link(self, envelope: dict[str, Any]) -> tuple[bool, int]:
        """File a used approval link for its site's node, once. ``(first_time, seq)``.

        The relay does not decide whether a link is valid: the node does, as for
        requests. It only makes sure one link is carried once, so a second click
        is answered "already used" instead of being sent again.
        """
        approval = envelope.get("approval") or {}
        site, link_id = str(approval.get("site") or ""), str(approval.get("id") or "")
        if not link_id or not re.fullmatch(r"[0-9a-f]{8,64}", link_id):
            raise ValueError("not an approval link")
        used_path = self._file(site, "links").with_suffix(".json")
        with self._lock:
            used = json.loads(used_path.read_text(encoding="utf-8")) if used_path.exists() else {}
            if link_id in used:
                return False, int(used[link_id])
            used[link_id] = -1
            used_path.parent.mkdir(parents=True, exist_ok=True)
            used_path.write_text(json.dumps(used), encoding="utf-8")
        seq = self._append(site, "requests", {"approval": envelope,
                                              "posted_by": f"link:{approval.get('recipient', '')}"})
        with self._lock:
            used = json.loads(used_path.read_text(encoding="utf-8"))
            used[link_id] = seq
            used_path.write_text(json.dumps(used), encoding="utf-8")
        return True, seq


#: The longest a support session may be opened for.
MAX_SESSION_S = 8 * 3600
#: How long a long-poll waits for bytes before answering empty.
POLL_WAIT_S = 20.0


class SessionRelay:
    """Support sessions: two byte streams per session, recorded, time-boxed."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def _rec_path(self, sid: str) -> Path:
        return self.root / f"{sid}.cast"

    def _record(self, sess: dict[str, Any], kind: str, data: str) -> None:
        with self._rec_path(sess["id"]).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps([round(time.time() - sess["t0"], 3), kind, data]) + "\n")

    def open(self, site: str, *, node: str, duration_s: int, reason: str) -> dict[str, Any]:
        if not _SITE.fullmatch(site):
            raise ValueError(f"not a site name: {site!r}")
        if not 60 <= int(duration_s) <= MAX_SESSION_S:
            raise ValueError(f"a session lasts between a minute and {MAX_SESSION_S // 3600} hours")
        now = time.time()
        sess = {"id": uuid.uuid4().hex, "site": site, "node": node, "reason": reason[:200],
                "opened_at": now, "expires_at": now + int(duration_s), "closed_at": None, "closed_by": "",
                "t0": now, "in": [], "out": [], "attached": []}
        header = {"version": 2, "width": 120, "height": 40, "timestamp": int(now),
                  "title": f"support session {sess['id']} at {site}",
                  "env": {"SITE": site, "NODE": node, "REASON": sess["reason"]}}
        self._rec_path(sess["id"]).write_text(json.dumps(header) + "\n", encoding="utf-8")
        with self._lock:
            self._sessions[sess["id"]] = sess
        return self.describe(sess)

    @staticmethod
    def describe(sess: dict[str, Any]) -> dict[str, Any]:
        return {k: sess[k] for k in ("id", "site", "node", "reason", "opened_at", "expires_at",
                                     "closed_at", "closed_by")} | {"open": SessionRelay.is_open(sess)}

    @staticmethod
    def is_open(sess: dict[str, Any]) -> bool:
        return sess["closed_at"] is None and time.time() < sess["expires_at"]

    def get(self, sid: str) -> dict[str, Any]:
        sess = self._sessions.get(sid)
        if sess is None:
            raise KeyError(sid)
        if sess["closed_at"] is None and time.time() >= sess["expires_at"]:
            self.close(sid, by="the time limit")
        return sess

    def list(self, site: str | None = None) -> list[dict[str, Any]]:
        return [self.describe(self.get(sid)) for sid, s in list(self._sessions.items())
                if site is None or s["site"] == site]

    def write(self, sid: str, direction: str, data: bytes, *, by: str = "") -> int:
        sess = self.get(sid)
        if not self.is_open(sess):
            raise PermissionError("this session has ended")
        text = data.decode("utf-8", errors="replace")
        with self._lock:
            sess[direction].append(data)
            n = len(sess[direction])
        self._record(sess, "i" if direction == "in" else "o", text)
        if by and direction == "in" and by not in sess["attached"]:
            sess["attached"].append(by)
            self._record(sess, "m", f"operator attached: {by}")
        return n

    async def read(self, sid: str, direction: str, after: int, *, wait_s: float = POLL_WAIT_S) -> dict[str, Any]:
        deadline = time.monotonic() + wait_s
        while True:
            sess = self.get(sid)
            chunks = sess[direction][after:]
            if chunks or not self.is_open(sess) or time.monotonic() >= deadline:
                return {"data": base64.b64encode(b"".join(chunks)).decode(), "next": after + len(chunks),
                        "open": self.is_open(sess), "closed_by": sess["closed_by"]}
            await asyncio.sleep(0.05)

    def close(self, sid: str, *, by: str) -> dict[str, Any]:
        sess = self._sessions[sid]
        if sess["closed_at"] is None:
            sess["closed_at"] = time.time()
            sess["closed_by"] = by
            self._record(sess, "m", f"session closed by {by}")
        return self.describe(sess)

    def recording(self, sid: str) -> str:
        self.get(sid)
        return self._rec_path(sid).read_text(encoding="utf-8")


def _operators() -> set[str]:
    return {p.strip() for p in os.environ.get(OPERATORS_ENV, "").split(",") if p.strip()}


def build_maintenance_router(box: MaintenanceBox, relay: "SessionRelay | None" = None):
    from fastapi import APIRouter, HTTPException, Query, Request
    from fastapi.responses import PlainTextResponse

    from .tenancy import TenancyPolicy

    router = APIRouter()
    policy = TenancyPolicy.from_env()

    def _grant(request: Request):
        return policy.grant_for(request)

    def _is_operator(request: Request) -> bool:
        # Named in the allowlist, as the edge's export names its downstream.
        # The serving layer reads a handle's context (`@ops:org`) as a site, so
        # "has a site" cannot tell an operator from a node; the list can.
        grant = _grant(request)
        return bool(grant.principal) and grant.principal in _operators()

    def _node_site(request: Request) -> str:
        grant = _grant(request)
        if not grant.site or _is_operator(request):
            raise HTTPException(status_code=403, detail="only a site's own node reads or answers its maintenance requests")
        return grant.site

    def _operator(request: Request) -> str:
        if not _is_operator(request):
            raise HTTPException(status_code=403, detail="only a listed maintenance operator may do this")
        return _grant(request).principal

    @router.post("/maintenance/requests")
    def post_request(request: Request, envelope: dict) -> dict:
        by = _operator(request)
        try:
            seq = box.add_request(envelope, by=by)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"seq": seq}

    @router.get("/maintenance/requests")
    def get_requests(request: Request, after: int = Query(0, ge=0), limit: int = Query(MAX_PAGE, ge=1, le=MAX_PAGE)) -> dict:
        site = _node_site(request)
        return {"site": site, "requests": box.requests(site, after=after, limit=limit)}

    @router.post("/maintenance/results")
    def post_results(request: Request, body: dict) -> dict:
        site = _node_site(request)
        results = body.get("results")
        if not isinstance(results, list):
            raise HTTPException(status_code=422, detail="expected {'results': [...]}")
        return {"seq": box.add_results(site, results)}

    @router.get("/maintenance/results")
    def get_results(request: Request, site: str = Query(...), after: int = Query(0, ge=0)) -> dict:
        _operator(request)
        try:
            return {"site": site, "results": box.results(site, after=after)}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    relay = relay if relay is not None else SessionRelay(box.root / "sessions")

    def _session(sid: str) -> dict[str, Any]:
        try:
            return relay.get(sid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="no such session") from exc

    def _party(request: Request, sid: str) -> tuple[str, dict[str, Any]]:
        """("node", s) for the session's own site's node, ("operator", s) for a listed operator."""
        sess = _session(sid)
        if _is_operator(request):
            return "operator", sess
        if _grant(request).site != sess["site"]:
            raise HTTPException(status_code=404, detail="no such session")
        return "node", sess

    @router.post("/maintenance/sessions")
    def open_session(request: Request, body: dict) -> dict:
        site = _node_site(request)  # only the site's own node opens one
        grant = _grant(request)
        try:
            return relay.open(site, node=str(body.get("node") or grant.principal or ""),
                              duration_s=int(body.get("duration_s") or 0), reason=str(body.get("reason") or ""))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/maintenance/sessions")
    def list_sessions(request: Request, site: str = Query(...)) -> dict:
        _operator(request)
        return {"sessions": relay.list(site)}

    @router.get("/maintenance/sessions/{sid}")
    def show_session(request: Request, sid: str) -> dict:
        _, sess = _party(request, sid)
        return relay.describe(sess)

    @router.post("/maintenance/sessions/{sid}/send")
    def send(request: Request, sid: str, body: dict) -> dict:
        party, sess = _party(request, sid)
        try:
            data = base64.b64decode(str(body.get("data") or ""), validate=True)
            n = relay.write(sid, "out" if party == "node" else "in", data,
                            by="" if party == "node" else _grant(request).principal or "")
        except PermissionError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="data must be base64") from exc
        return {"n": n}

    @router.get("/maintenance/sessions/{sid}/recv")
    async def recv(request: Request, sid: str, after: int = Query(0, ge=0),
                   wait_s: float = Query(POLL_WAIT_S, ge=0, le=POLL_WAIT_S)) -> dict:
        party, _ = _party(request, sid)
        return await relay.read(sid, "in" if party == "node" else "out", after, wait_s=wait_s)

    @router.post("/maintenance/sessions/{sid}/close")
    def close(request: Request, sid: str) -> dict:
        party, _ = _party(request, sid)
        return relay.close(sid, by="the site" if party == "node" else (_grant(request).principal or "operator"))

    @router.get("/maintenance/sessions/{sid}/recording")
    def recording(request: Request, sid: str) -> PlainTextResponse:
        _party(request, sid)
        return PlainTextResponse(relay.recording(sid), media_type="application/x-asciicast")

    return router


def build_approval_router(box: MaintenanceBox):
    """``/approve/<token>`` — where a person's approval link lands. Public: the link is the credential.

    Opening the link (GET) only shows what it decides and a button: mail
    scanners and link previews fetch every URL in a message, so a GET that
    acted would approve things nobody read. Pressing the button (POST) files
    the link for the site's node, once. The node verifies it (signature, site,
    expiry, recipient, single use, and the exact request) and decides.
    """
    import html as _html

    from fastapi import APIRouter, HTTPException
    from fastapi.responses import HTMLResponse

    from axiom.infra.maintenance import read_link_token

    router = APIRouter()

    def _envelope(token: str) -> dict[str, Any]:
        try:
            envelope = read_link_token(token)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="not an approval link") from exc
        site = str(envelope["approval"].get("site") or "")
        if not _SITE.fullmatch(site):
            raise HTTPException(status_code=404, detail="not an approval link")
        return envelope

    def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(status_code=status, content=(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{_html.escape(title)}</title>"
            "<body style='font:16px system-ui;max-width:36rem;margin:3rem auto;padding:0 1rem'>"
            f"<h1 style='font-size:1.3rem'>{_html.escape(title)}</h1>{body}</body>"))

    @router.get("/approve/{token}")
    def show(token: str) -> HTMLResponse:
        envelope = _envelope(token)
        a = envelope["approval"]
        req = (box.find_request(a["site"], str(a.get("request_id"))) or {}).get("request") or {}
        what = req.get("action", "a maintenance request")
        params = ", ".join(f"{k}={v}" for k, v in (req.get("params") or {}).items())
        verb = "Approve" if a.get("decision") == "approve" else "Decline"
        return _page(f"{verb} {what} at {a['site']}?", (
            f"<p>For {_html.escape(str(a.get('recipient', '')))}. "
            f"Action: <b>{_html.escape(str(what))}</b>{' (' + _html.escape(params) + ')' if params else ''}.</p>"
            f"<p>Valid until {_html.escape(str(a.get('expires_at', '')))}. This link works once.</p>"
            f"<form method=post><button style='font-size:1rem;padding:.6rem 1.2rem'>{verb}</button></form>"))

    @router.post("/approve/{token}")
    def use(token: str) -> HTMLResponse:
        envelope = _envelope(token)
        try:
            first, _seq = box.add_link(envelope)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="not an approval link") from exc
        if not first:
            return _page("This link has already been used", "<p>Nothing more was sent.</p>", status=409)
        return _page("Sent to the site's node",
                     "<p>The node checks the link and acts on its next check-in, usually within a minute. "
                     "The result is reported back to the platform.</p>")

    return router


__all__ = ["DIR_ENV", "MAX_SESSION_S", "OPERATORS_ENV", "MaintenanceBox", "SessionRelay",
           "build_approval_router", "build_maintenance_router"]
