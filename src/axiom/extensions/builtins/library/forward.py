# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A bound install's library is its site node's library (ADR-207, ADR-212).

When the one serving mode says *bound*, every library call (the browser's, the
CLI's, an agent's) goes to the bound node's ``/api/v1/library`` with the
binding's key, attached here from the vault and offered only to that node. This
install keeps no copy: no bytes, no catalog row, no chunks. The node verifies the
key, derives the site and principal from it, and files the document exactly as
it would for someone signed in there, so a laptop cannot file under a site its
key does not hold.

A node that cannot be reached is a 503 that names the site and the node. Nothing
is queued locally and nothing local is substituted: a document a person thinks
they filed with their team, but which sits on their laptop, is the failure this
module exists to make impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from axiom.extensions.builtins.library import limits

API_BASE = "/api/v1/library"
#: Multipart framing and form fields on top of the batch cap.
_FRAMING_BYTES = 1024 * 1024
#: Headers worth carrying back from the node to the browser.
_PASS_BACK = ("content-type", "content-disposition", "x-content-type-options", "content-security-policy", "cache-control")


@dataclass(frozen=True)
class BoundNode:
    site: str
    face_url: str
    node: str


class NodeUnreachable(RuntimeError):
    def __init__(self, message: str, *, status: int = 503) -> None:
        super().__init__(message)
        self.status = status


def bound_node() -> BoundNode | None:
    """The node this install is bound to, or ``None`` when the library is local."""
    try:
        from axiom.infra.serving_mode import resolve_mode

        mode = resolve_mode()
    except Exception:  # noqa: BLE001 - an unreadable mode is local, and says so elsewhere
        return None
    if getattr(mode, "mode", "") != "bound" or not getattr(mode, "face_url", ""):
        return None
    return BoundNode(site=mode.site, face_url=mode.face_url.rstrip("/"), node=mode.node or mode.face_url)


def _key(face_url: str) -> str:
    try:
        from axiom.extensions.builtins.data_platform.read_target import bound_key_for

        return bound_key_for(face_url)
    except Exception:  # noqa: BLE001 - no vault is no key; the node answers 401 and says so
        return ""


def request(
    node: BoundNode,
    method: str,
    subpath: str,
    *,
    params: Any = None,
    content: bytes | None = None,
    json: Any = None,
    files: Any = None,
    data: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 120.0,
):
    """One call to the bound node's library. Returns the ``httpx.Response``."""
    import httpx

    sent = dict(headers or {})
    key = _key(node.face_url)
    if key:
        sent["authorization"] = f"Bearer {key}"
    url = f"{node.face_url}{API_BASE}{subpath}"
    try:
        return httpx.request(
            method, url, params=params, content=content, json=json, files=files, data=data,
            headers=sent, timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise NodeUnreachable(
            f"this install is bound to {node.site} at {node.node}, which holds its library, and that "
            f"node could not be reached ({type(exc).__name__}). Nothing was filed locally."
        ) from None


async def forward_http(request_in: Any, node: BoundNode):
    """Relay one browser request to the bound node, body and answer verbatim."""
    from starlette.responses import JSONResponse, Response

    path = request_in.url.path
    sub = path[len(API_BASE):] if path.startswith(API_BASE) else path
    declared = int(request_in.headers.get("content-length") or 0)
    if declared > limits.max_batch_bytes() + _FRAMING_BYTES:
        return JSONResponse(
            {"detail": f"this upload is larger than the {limits.format_bytes(limits.max_batch_bytes())} one request may carry"},
            status_code=413,
        )
    body = await request_in.body()
    headers = {}
    if request_in.headers.get("content-type"):
        headers["content-type"] = request_in.headers["content-type"]
    try:
        answer = request(
            node, request_in.method, sub, params=list(request_in.query_params.multi_items()),
            content=body or None, headers=headers,
        )
    except NodeUnreachable as exc:
        return JSONResponse({"detail": str(exc), "code": "node_unreachable"}, status_code=exc.status)
    out = Response(content=answer.content, status_code=answer.status_code)
    for name in _PASS_BACK:
        if name in answer.headers:
            out.headers[name] = answer.headers[name]
    out.headers["x-axiom-served-by"] = node.node
    return out


def call_json(node: BoundNode, method: str, subpath: str, **kw: Any) -> tuple[int, Any]:
    """A skill's call to the node: ``(status, decoded JSON body)``."""
    answer = request(node, method, subpath, **kw)
    try:
        payload = answer.json()
    except ValueError:
        payload = {"detail": answer.text[:500]}
    return answer.status_code, payload


__all__ = ["API_BASE", "BoundNode", "NodeUnreachable", "bound_node", "call_json", "forward_http", "request"]
