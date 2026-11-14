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

    # The HTTP face hands over the caller the serving stack verified (its credential's site).
    verified = getattr(ctx, "library_caller", None)
    return verified if verified is not None else caller_from_skill(ctx, params)


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
    except (NotFound, Forbidden, UploadRefused, CallerUnknown, NodeUnreachable, ValueError, PermissionError) as exc:
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
        fields = {k: str(params[k]) for k in ("visibility", "title", "collection", "ec_label", "retention_class")
                  if params.get(k)}
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
            ec_label=fields.get("ec_label"), retention_class=fields.get("retention_class"),
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


def status(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """What happened to one file: every stage, extraction, route, the EC verdict and where it filed (ADR-213)."""
    from axiom.extensions.builtins.library import service

    return _one("GET", "/pipeline", service.item_pipeline, params, ctx)


def reextract(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.extensions.builtins.library import pipeline

    extractors = params.get("extractors")
    if isinstance(extractors, str):
        extractors = [e.strip() for e in extractors.split(",") if e.strip()]
    stale = bool(params.get("stale_only"))
    body = {"extractors": extractors or None, "stale_only": stale}
    return _one(
        "POST", "/reextract",
        lambda caller, item_id: pipeline.start_reextract(caller, item_id, extractors=extractors or None, stale_only=stale),
        params, ctx, json_body=body,
    )


def move(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.extensions.builtins.library import service

    folder = str(params.get("folder") or "").strip()
    if not folder:
        return SkillResult(ok=False, errors=["missing required param: folder"])
    return _one("POST", "/move", lambda caller, item_id: service.move_item(caller, item_id, folder), params, ctx,
                json_body={"folder": folder})


def _decision(params: dict[str, Any], ctx, verb: str, fn) -> SkillResult:
    reason = " ".join(str(params.get("reason") or "").split())
    if not reason:
        return SkillResult(ok=False, errors=["missing required param: reason (one line saying why)"])
    return _one("POST", f"/ec/{verb}", lambda caller, item_id: fn(caller, item_id, reason), params, ctx,
                json_body={"reason": reason})


def ec_status(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Why a file is held, its review trail, and whether you may release it (ADR-213 D9)."""
    from axiom.extensions.builtins.library import ec_review

    return _one("GET", "/ec", ec_review.state, params, ctx)


def ec_release(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Release a held file version for retrieval: an ec-reviewer, never its uploader, with a reason."""
    from axiom.extensions.builtins.library import ec_review

    return _decision(params, ctx, "release", ec_review.release)


def ec_keep_held(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Record that a reviewer looked and keeps the file held, with a reason."""
    from axiom.extensions.builtins.library import ec_review

    return _decision(params, ctx, "keep-held", ec_review.keep_held)


def ec_rehold(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Take a release back: the file leaves retrieval and is held again, with a reason."""
    from axiom.extensions.builtins.library import ec_review

    return _decision(params, ctx, "re-hold", ec_review.rehold)


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


__all__ = ["access", "delete", "ec_keep_held", "ec_rehold", "ec_release", "ec_status", "label", "list_items", "purge",
           "request", "restore", "search", "share", "show", "transfer", "update", "upload"]


def label(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """MVP-22: what a document's label is, or confirm / change it and its retention class."""
    from axiom.extensions.builtins.library import service

    if str(params.get("sweep") or "").lower() in ("1", "true", "yes"):
        return _guarded(lambda: SkillResult(ok=True, value=service.sweep_unlabelled(_caller(ctx, params))))
    changes = {k: params[k] for k in ("label", "retention_class") if params.get(k) not in (None, "")}
    if not changes:
        def answer(caller, item_id):
            item = service.get_item(caller, item_id)
            return {"id": item["id"], "title": item["title"], "ec_label": item["ec_label"],
                    "retention": item["retention"], "sample": item["sample"]}

        return _one("GET", "", answer, params, ctx)
    release = str(params.get("release_hold") or "").lower() in ("1", "true", "yes")
    return _one(
        "POST", "/label",
        lambda caller, item_id: service.label_item(caller, item_id, release_hold=release, **changes),
        params, ctx, json_body={**changes, "release_hold": release},
    )


def purge(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """MVP-22-A2: delete forever, only after the floor and outside any hold (``operations.purge_item``)."""
    from axiom.extensions.builtins.library import operations

    return _one("POST", "/purge", operations.purge_item, params, ctx)


# ------------------------------------------------------------------ sharing (ADR-263)


def _ref(params: dict[str, Any]) -> str:
    return str(params.get("id") or params.get("item_id") or params.get("target") or "").strip()


def _bool(raw: Any) -> bool | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _sharing_call(params: dict[str, Any], ctx, local, *, method: str = "POST", suffix: str = "",
                  body: dict | None = None) -> SkillResult:
    ref = _ref(params)
    if not ref:
        return SkillResult(ok=False, errors=["missing required param: id (a file id, or folder:<path>)"])

    def go() -> SkillResult:
        node = _node()
        if node is not None:
            from axiom.extensions.builtins.library.forward import call_json

            if ref.startswith("folder:"):
                return SkillResult(ok=False, errors=["sharing a folder on a bound install happens on its node"])
            return _from_node(*call_json(node, method, f"/items/{ref}{suffix}", json=body))
        return SkillResult(ok=True, value=local(_caller(ctx, params), ref))

    return _guarded(go)


def share(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Share a file or folder (``--with``/``--role``/``--expires``), remove someone, set general access or settings."""
    from axiom.extensions.builtins.library import sharing

    who = str(params.get("with") or "").strip()
    general = str(params.get("general") or "").strip()
    remove = bool(_bool(params.get("remove")))
    ecs, vcd = _bool(params.get("editors_can_share")), _bool(params.get("viewers_can_download"))
    if not (who or general or ecs is not None or vcd is not None):
        return SkillResult(ok=False, errors=["say who to share with (with), general access, or a setting"])

    def local(caller, ref):
        out: dict[str, Any] = {}
        if who and remove:
            out["removed"] = sharing.unshare(caller, ref, who)
        elif who:
            out["shared"] = sharing.share(caller, ref, who, params.get("role") or "viewer",
                                          expires=params.get("expires"), notify=_bool(params.get("notify")) is not False,
                                          message=str(params.get("message") or ""))
        if general:
            out["general"] = sharing.general(caller, ref, general)
        if ecs is not None or vcd is not None:
            out["settings"] = sharing.settings(caller, ref, editors_can_share=ecs, viewers_can_download=vcd)
        if len(out) == 1:
            return next(iter(out.values()))
        return out

    body = {k: params[k] for k in ("with", "role", "expires", "remove", "general", "editors_can_share",
                                   "viewers_can_download", "notify", "message") if k in params}
    return _sharing_call(params, ctx, local, suffix="/share", body=body)


def access(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Who has access to a file or folder, and why; what you may do."""
    from axiom.extensions.builtins.library import sharing

    return _sharing_call(params, ctx, sharing.who_has_access, method="GET", suffix="/access")


def transfer(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Make someone already on the access list the Owner; you become an Editor."""
    from axiom.extensions.builtins.library import sharing

    to = str(params.get("to") or "").strip()
    if not to:
        return SkillResult(ok=False, errors=["missing required param: to (@name:<site>)"])
    return _sharing_call(params, ctx, lambda caller, ref: sharing.transfer(caller, ref, to),
                         suffix="/transfer", body={"to": to})


REQUEST_ACTIONS = ("ask", "status", "withdraw", "remind", "list", "approve", "decline")


def request(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Ask for access, see or withdraw your request; or, for an Owner, list, approve or decline requests."""
    from axiom.extensions.builtins.library import sharing

    action = str(params.get("action") or "ask").strip().lower()
    if action not in REQUEST_ACTIONS:
        return SkillResult(ok=False, errors=[f"action is one of {', '.join(REQUEST_ACTIONS)}"])
    rid = str(params.get("request_id") or "").strip()
    if action in ("withdraw", "remind", "approve", "decline") and not rid:
        return SkillResult(ok=False, errors=["missing required param: request_id"])

    def local(caller, ref):
        if action == "ask":
            return sharing.request(caller, ref, params.get("role") or "viewer", note=str(params.get("note") or ""))
        if action == "status":
            return sharing.my_request(caller, ref)
        if action == "withdraw":
            return sharing.withdraw(caller, ref, rid)
        if action == "remind":
            return sharing.remind(caller, ref, rid)
        if action == "list":
            return sharing.requests(caller, ref)
        return sharing.answer(caller, ref, rid, approve=action == "approve", role=params.get("role"),
                              reason=str(params.get("reason") or ""))

    body = {k: params[k] for k in ("action", "role", "note", "request_id", "reason") if k in params}
    return _sharing_call(params, ctx, local, suffix="/requests", body=body)
