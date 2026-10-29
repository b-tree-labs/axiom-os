# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The library verbs as skills: one implementation behind the CLI, MCP and agent tools.

Each verb serves from this install's library, or, when the install is bound,
asks the bound node's ``/api/v1/library`` with the binding's key and returns the
node's answer (ADR-212). The HTTP face calls :mod:`..service` directly with the
caller the serving stack verified; these call it with the skill context's
principal.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _caller(ctx: SkillContext | None, params: dict[str, Any]):
    from axiom.extensions.builtins.library.scope import caller_from_skill

    return caller_from_skill(ctx, params)


def _node():
    from axiom.extensions.builtins.library.forward import bound_node

    return bound_node()


def _from_node(status: int, payload: Any, *, key: str | None = None) -> SkillResult:
    if status >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        return SkillResult(ok=False, errors=[f"the bound node answered {status}: {detail}"])
    return SkillResult(ok=True, value=payload if key is None else payload.get(key), actions_taken=["asked the bound node"])


def _guarded(fn) -> SkillResult:
    from axiom.extensions.builtins.library.forward import NodeUnreachable
    from axiom.extensions.builtins.library.limits import UploadRefused
    from axiom.extensions.builtins.library.scope import CallerUnknown
    from axiom.extensions.builtins.library.service import Forbidden, NotFound

    try:
        return fn()
    except (NotFound, Forbidden, UploadRefused, CallerUnknown, NodeUnreachable, ValueError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])


def _read_upload(params: dict[str, Any]) -> tuple[str, bytes]:
    path = str(params.get("path") or "").strip()
    if path:
        source = Path(path).expanduser()
        if not source.is_file():
            raise ValueError(f"no file at {path}")
        from axiom.extensions.builtins.library import limits

        if source.stat().st_size > limits.max_file_bytes():
            limits.check_size(source.name, source.stat().st_size)
        return str(params.get("file_name") or source.name), source.read_bytes()
    encoded = str(params.get("content_base64") or "")
    name = str(params.get("file_name") or "").strip()
    if not encoded or not name:
        raise ValueError("give `path`, or `file_name` with `content_base64`")
    try:
        return name, base64.b64decode(encoded, validate=True)
    except ValueError:
        raise ValueError("content_base64 is not valid base64") from None


def upload(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    def go() -> SkillResult:
        name, data = _read_upload(params)
        fields = {k: str(params[k]) for k in ("visibility", "title", "collection") if params.get(k)}
        tags = params.get("tags")
        if tags:
            fields["tags"] = ",".join(tags) if isinstance(tags, list) else str(tags)
        node = _node()
        if node is not None:
            from axiom.extensions.builtins.library.forward import call_json

            status, payload = call_json(node, "POST", "/items", files=[("files", (name, data))], data=fields)
            result = _from_node(status, payload)
            if result.ok and payload.get("refused") and not payload.get("items"):
                return SkillResult(ok=False, errors=[r["reason"] for r in payload["refused"]])
            return result
        from axiom.extensions.builtins.library import service

        filed = service.upload(
            _caller(ctx, params), name, data, visibility=fields.get("visibility"), title=fields.get("title"),
            tags=fields.get("tags"), collection=fields.get("collection"),
        )
        verb = "already filed" if filed.duplicate else "filed"
        return SkillResult(
            ok=True,
            value={**filed.item, "duplicate": filed.duplicate, "warnings": filed.warnings},
            actions_taken=[f"{verb} {filed.item['file_name']} under {filed.item['category']} ({filed.item['index_status']})"],
        )

    return _guarded(go)


def list_items(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    keys = ("q", "category", "collection", "tag", "visibility", "status")
    query = {k: str(params[k]) for k in keys if params.get(k)}
    deleted = bool(params.get("deleted"))
    limit = int(params.get("limit") or 200)

    def go() -> SkillResult:
        node = _node()
        if node is not None:
            from axiom.extensions.builtins.library.forward import call_json

            return _from_node(*call_json(node, "GET", "/items", params={**query, "deleted": deleted, "limit": limit}))
        from axiom.extensions.builtins.library import service

        caller = _caller(ctx, params)
        items = service.list_items(caller, deleted=deleted, limit=limit, **query)
        return SkillResult(ok=True, value={"items": items, "site": caller.site})

    return _guarded(go)


def _one(method: str, suffix: str, call, params: dict[str, Any], ctx, *, json_body: dict | None = None) -> SkillResult:
    item_id = str(params.get("id") or params.get("item_id") or "").strip()
    if not item_id:
        return SkillResult(ok=False, errors=["missing required param: id"])

    def go() -> SkillResult:
        node = _node()
        if node is not None:
            from axiom.extensions.builtins.library.forward import call_json

            return _from_node(*call_json(node, method, f"/items/{item_id}{suffix}", json=json_body))
        return SkillResult(ok=True, value=call(_caller(ctx, params), item_id))

    return _guarded(go)


def show(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.extensions.builtins.library import service

    return _one("GET", "", service.get_item, params, ctx)


def update(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.extensions.builtins.library import service

    changes: dict[str, Any] = {}
    for key in ("title", "collection", "visibility"):
        if params.get(key) is not None:
            changes[key] = str(params[key])
    if params.get("tags") is not None:
        tags = params["tags"]
        changes["tags"] = tags if isinstance(tags, list) else [t for t in str(tags).split(",") if t.strip()]
    if not changes:
        return SkillResult(ok=False, errors=["nothing to change: give title, tags, collection or visibility"])
    return _one(
        "PATCH", "", lambda caller, item_id: service.update_item(caller, item_id, **changes), params, ctx,
        json_body=changes,
    )


def delete(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.extensions.builtins.library import service

    return _one("DELETE", "", service.delete_item, params, ctx)


def restore(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.extensions.builtins.library import service

    return _one("POST", "/restore", service.restore_item, params, ctx)


def search(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    q = str(params.get("q") or params.get("query") or "").strip()
    if not q:
        return SkillResult(ok=False, errors=["missing required param: q"])
    limit = int(params.get("limit") or 20)

    def go() -> SkillResult:
        node = _node()
        if node is not None:
            from axiom.extensions.builtins.library.forward import call_json

            return _from_node(*call_json(node, "GET", "/search", params={"q": q, "limit": limit}))
        from axiom.extensions.builtins.library import service

        return SkillResult(ok=True, value={"q": q, "results": service.search(_caller(ctx, params), q, limit=limit)})

    return _guarded(go)


__all__ = ["delete", "list_items", "restore", "search", "show", "update", "upload"]
