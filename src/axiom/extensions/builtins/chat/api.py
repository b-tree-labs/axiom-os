# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The chat extension's slice of ``/api/v1`` — conversation CRUD (C2 ②).

Contributed through the webapp api-surface registry (manifest ``kind = "api"``,
``subpath = "/chat"``), so a client talks to one surface with one auth posture
and ``webapp`` never imports chat. Conversations live in the shared,
site/tenant-scoped ``DatabaseSessionStore``; message streaming is the SSE
sibling (``chat_stream``, C2 ③).

Scope: reads and writes are bounded by :mod:`axiom.infra.site_scope` — a site
this request may not see is 404, never 403 (see that module). appkit's
``account_id`` is the tenant axis; it maps to ``tenant_id`` here at the boundary.
Ownership (the acting principal) narrows within a scope; full principal-grant
enforcement rides the authz seam, and ``site_scope`` is the bound until it lands.
"""

import logging
from typing import Any

# NOTE: no `from __future__ import annotations` here on purpose. FastAPI resolves
# each handler's parameter annotations to decide body-vs-query; stringized
# annotations for the locally-defined Request/Pydantic models resolve to nothing
# and every body silently becomes a query param (422). Real annotation objects
# (evaluated at def time) are what FastAPI needs.

#: Seconds of silence after which the stream sends a keepalive comment.
KEEPALIVE_S = 8.0

#: Test seam: production reads/writes through ``session_for("chat")``; a router
#: test injects a SQLite engine so the surface is exercised without Postgres.
_TEST_ENGINE: Any | None = None


def set_test_engine(engine: Any | None) -> None:
    global _TEST_ENGINE
    _TEST_ENGINE = engine


def _store():
    # Honest backend selection: injected engine (tests) > configured database >
    # zero-config local SQLite. See conversation_store.resolve_conversation_store.
    from axiom.infra.orchestrator.conversation_store import resolve_conversation_store

    return resolve_conversation_store(_TEST_ENGINE)


def _default_turn_runner(session, user_input: str, render, *, tenant_id: str | None = None) -> str:
    """Run one turn of the real agent, streaming through ``render``.

    Uses HeadlessChat — the serving surface's own construction — so this request
    gets a properly-guarded scope (no operator-local prompts, no transcript
    indexing, shared corpora, a turn deadline) that turn() will accept. The SSE
    render is set on the agent so text AND tool/action/approval events all flow
    onto the wire; turn() appends the user + assistant (+ tool) messages to the
    scope's session, which is the same object the caller persists.
    """
    import os

    from axiom.extensions.builtins.chat.headless import HeadlessChat
    from axiom.infra.gateway import Gateway

    # A browser-held turn gets a seconds-scale bound (HeadlessChat refuses
    # to default this — the surface owns it). Env-tunable for slow local
    # models; the SSE stream carries an in-band error if it trips.
    deadline = float(os.environ.get("AXIOM_CHAT_TURN_DEADLINE", "120"))
    gateway = Gateway()
    # ADR-207: the browser's chat answers with the model the terminal would, the bound node's when
    # this install is bound (the same resolution ``--ask`` makes). Only the bound default: a node
    # serving its own chat keeps the providers it was configured with.
    from axiom.extensions.builtins.chat.bound_model import apply_bound_model

    apply_bound_model(gateway)
    headless = HeadlessChat(gateway=gateway, turn_deadline=deadline)
    headless.agent.set_render_provider(render)
    scope = headless.new_scope(session=session, tenant_id=tenant_id)
    # The person's screen, for this turn's prompt only (dual drive). Read from the request's
    # context rather than passed, so a runner swapped in through ``set_turn_runner`` keeps its
    # signature; it never reaches the session's messages.
    from axiom.extensions.builtins.chat.view_drive import REQUEST_VIEW, parse_view_context

    scope.view_context = parse_view_context(REQUEST_VIEW.get())
    return headless.turn(user_input, scope=scope)


#: Test seam: a fake runner drives ``render`` with canned frames + appends to the
#: session, so the endpoint is exercised without a live LLM gateway.
_turn_runner = _default_turn_runner


def set_turn_runner(runner) -> None:
    global _turn_runner
    _turn_runner = runner


def reset_turn_runner() -> None:
    global _turn_runner
    _turn_runner = _default_turn_runner


log = logging.getLogger(__name__)

_ADOPTED: set[tuple[str, str]] = set()


def _principal(request) -> str:
    """The acting principal: the ADR-020 handle of the browser gate session.

    The handle the authz layer verified wins; otherwise it is derived from the
    session cookie exactly as the authz layer derives it. Never the raw ``sub``:
    an email is not a handle, and the tool authority denies a call it cannot
    parse. Conversations filed under the raw ``sub`` by earlier releases are
    re-owned to the handle the first time that person is seen.

    Empty means "no ownership narrowing" (the store treats it as the CLI does),
    which is safe because ``site_scope`` still bounds every read by site.
    """
    state = getattr(request, "state", None)
    verified = getattr(state, "principal", "") if state is not None else ""
    verified = verified if isinstance(verified, str) and verified.startswith("@") else ""
    try:
        from axiom.webauth import session_from_cookies

        claims = session_from_cookies(dict(request.cookies)) or {}
    except Exception:  # noqa: BLE001 - auth is best-effort here; scope is the bound
        return verified
    sub = str(claims.get("sub") or "")
    if not sub:
        return verified
    from axiom.extensions.builtins.http.authz_hook import session_handle

    handle = verified or session_handle(sub)
    _adopt_legacy_owner(sub, handle)
    return handle


def _adopt_legacy_owner(sub: str, handle: str) -> None:
    """Move conversations owned by the bare ``sub`` to ``handle``, once per process."""
    if (sub, handle) in _ADOPTED:
        return
    try:
        moved = _store().reown(sub, handle)
        if moved:
            log.info("chat: re-owned %d conversation(s) to the session handle", moved)
        _ADOPTED.add((sub, handle))
    except Exception as exc:  # noqa: BLE001 - history follows on the next request
        log.warning("chat: could not re-own conversations to the session handle: %s", exc)


def _served_site(scope) -> str | None:
    """The site a single-site node serves. A dev box (unbounded) or a multi-site
    node returns None — reads then filter by scope rather than a single ``==``."""
    if not scope.unbounded and len(scope.sites) == 1:
        return next(iter(scope.sites))
    return None


def _conv(meta: dict) -> dict:
    """Store metadata → the appkit Conversation shape (tenant_id → account_id)."""
    return {
        "id": meta["id"],
        "title": meta["title"],
        "starred": meta["starred"],
        "archived": meta["archived"],
        "created_at": meta["created_at"],
        "updated_at": meta["updated_at"],
        "site_id": meta["site_id"],
        "account_id": meta["tenant_id"],
    }


#: What a resume needs to put the screen back: where the person was and what was open. Not the
#: prompt's description of it (``summary``, ``stable``, ``title``) and not what the page offered
#: (``actions``, ``flags``, ``binding``): the page declares those again when it opens.
RESUME_VIEW_KEYS = (
    "schema",
    "app",
    "route",
    "screen",
    "site",
    "focus",
    "groups",
    "selection",
    "views",
    "truncated",
)


def remember_view(session, view_context) -> None:
    """Keep where the turn was asked from on the session, so a resume restores it (MVP-05-A2).

    Session-level state (``Session.context``, the payload column), never a message: the
    transcript stays what the person said, and the prompt still reads the full view per
    request. Only the restore keys are kept (:data:`RESUME_VIEW_KEYS`), only the newest, and a
    turn that sent none leaves the last one standing.
    """
    from .view_drive import parse_view_context

    view = parse_view_context(view_context)
    if view is None:
        return
    keep = {k: view[k] for k in RESUME_VIEW_KEYS if k in view}
    session.context = {**(session.context or {}), "view_context": keep}


def _history(params: dict) -> dict:
    import logging

    from fastapi import HTTPException

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    from .skills.history import NAME, SPEC

    registry = SkillRegistry()
    registry.register_skill(SPEC, mutating=False)
    ctx = SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.chat")
    )
    result = invoke_capability(registry, NAME, params, ctx, surface=WEB_SURFACE)
    if not result.ok:
        raise HTTPException(401, "; ".join(result.errors))
    return result.value


def register_routes(router: Any, *, subpath: str = "/chat") -> None:
    """Attach the chat conversation endpoints under ``/api/v1<subpath>``."""
    import asyncio
    import json

    from fastapi import HTTPException, Query, Request, Response
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel

    from axiom.infra import site_scope

    class CreateBody(BaseModel):
        account_id: str | None = None
        title: str | None = None

    class UpdateBody(BaseModel):
        title: str | None = None
        starred: bool | None = None
        account_id: str | None = None
        #: False restores a soft-deleted (archived) conversation.
        archived: bool | None = None

    class SendBody(BaseModel):
        content: str = ""
        view_context: Any | None = None
        document_ids: list[str] | None = None
        compose_session_id: str | None = None

    class GuestBody(BaseModel):
        message: str = ""
        region: str | None = None
        conversation_id: str | None = None
        account_id: str | None = None

    class FeedbackBody(BaseModel):
        message_id: str
        rating: str = ""
        comment: str | None = None

    def _sse(frame: dict) -> str:
        return f"data: {json.dumps(frame)}\n\n"

    def _stream_turn(
        session, content, conversation_id, store, *, persist, tenant_id=None, view_context=None
    ):
        """Drive one turn in a worker thread, streaming its frames as SSE.

        The turn is CPU/IO-heavy and blocking, so it runs in the executor; the
        SseRenderProvider emits frames from that thread onto an asyncio.Queue via
        call_soon_threadsafe, and the generator drains them. conversation_id is
        sent first so the client can adopt/keep the thread; an exception becomes
        a single error frame rather than a mid-stream 500; the turn is persisted
        (append-only) on success when `persist`."""
        from .providers.sse_render import SseRenderProvider

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        done = object()

        def emit(frame):
            loop.call_soon_threadsafe(q.put_nowait, frame)

        def work():
            from .view_drive import REQUEST_VIEW

            # Set in the worker thread, where the turn runs: an executor does not carry context.
            REQUEST_VIEW.set(view_context)
            try:
                _turn_runner(session, content, SseRenderProvider(emit), tenant_id=tenant_id)
                if persist:
                    remember_view(session, view_context)
                    store.save(session)
            except Exception as exc:  # noqa: BLE001 - surfaced as an error frame
                emit({"error": str(exc)})
            finally:
                loop.call_soon_threadsafe(q.put_nowait, done)

        async def gen():
            yield _sse({"conversation_id": conversation_id})
            fut = loop.run_in_executor(None, work)
            while True:
                # A node's model answers in one block after its own tool
                # rounds, so a turn can be silent for longer than a browser's
                # idle timeout. A comment line every few seconds says the turn
                # is alive without being a frame anybody renders.
                try:
                    frame = await asyncio.wait_for(q.get(), KEEPALIVE_S)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if frame is done:
                    break
                yield _sse(frame)
            await fut

        # Unbuffered and uncached, so a reverse proxy passes each frame and
        # keepalive on as it is sent, with no per-site configuration (#1710).
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def _require_visible(meta: dict | None, scope) -> dict:
        # Uniform 404 for both "missing" and "out of scope" — never disclose
        # that a hidden conversation exists (site_scope's 404-not-403 rule).
        if meta is None or not scope.permits(meta.get("site_id") or ""):
            raise HTTPException(404, "conversation not found")
        return meta

    @router.get(subpath + "/conversations", tags=["chat"])
    def list_conversations(
        request: Request,
        account_id: str | None = Query(None),
        search: str | None = Query(None),
        archived: str | None = Query(None, pattern="^(only|include)$"),
    ) -> dict:
        # `archived=only` is the Settings archive (what a soft delete put
        # away, to restore); `include` lists both. Default: active only.
        scope = site_scope.resolve()
        metas = _store().list_meta(
            site_id=_served_site(scope),
            tenant_id=account_id,
            principal=_principal(request),
            search=search,
            include_archived=archived == "include",
            archived_only=archived == "only",
        )
        convs = [_conv(m) for m in metas if scope.permits(m["site_id"])]
        return {"conversations": convs}

    @router.post(subpath + "/conversations", tags=["chat"], status_code=201)
    def create_conversation(request: Request, body: CreateBody) -> dict:
        scope = site_scope.resolve()
        store = _store()
        # An isolated variant's conversation belongs to its tenant, as its turns
        # already do (HeadlessChat.new_scope), so its History lists it (MVP-05-A1).
        from axiom.infra.variants import variant_tenant

        session = store.create(
            site_id=_served_site(scope) or "",
            tenant_id=variant_tenant() or body.account_id or "",
            principal_id=_principal(request),
        )
        # A title chosen up front is a person's name for it and is kept. The
        # client's placeholder ("New Chat") is not a choice: renaming to it
        # would lock every new chat under the placeholder forever.
        from axiom.infra.orchestrator.titling import DEFAULT_TITLE

        if body.title and body.title.strip() and body.title.strip() != DEFAULT_TITLE:
            store.rename(session.session_id, body.title)
        meta = store.conversation(session.session_id, _principal(request))
        return _conv(meta) if meta else {"id": session.session_id}

    @router.get(subpath + "/conversations/{conversation_id}", tags=["chat"])
    def get_conversation(
        request: Request,
        conversation_id: str,
        message_limit: int | None = Query(None),
        messages_before: str | None = Query(None),
    ) -> dict:
        scope = site_scope.resolve()
        detail = _store().get_detail(
            conversation_id,
            principal=_principal(request),
            message_limit=message_limit,
            messages_before=messages_before,
        )
        _require_visible(detail["conversation"] if detail else None, scope)
        detail["conversation"] = _conv(detail["conversation"])
        return detail

    @router.get(subpath + "/history", tags=["chat"])
    def history(
        request: Request,
        q: str = Query("", max_length=200),
        limit: int = Query(50, ge=1, le=200),
        archived: bool = Query(False),
    ) -> dict:
        # The History page: the ``chat.history`` skill through the dispatch
        # chokepoint, the same list the CLI and the MCP tool return.
        return _history(
            {
                "q": q,
                "limit": limit,
                "include_archived": archived,
                "_principal": _principal(request),
            }
        )

    @router.patch(subpath + "/conversations/{conversation_id}", tags=["chat"])
    def update_conversation(request: Request, conversation_id: str, body: UpdateBody) -> dict:
        scope = site_scope.resolve()
        principal = _principal(request)
        store = _store()
        _require_visible(store.conversation(conversation_id, principal), scope)
        if body.title is not None:
            store.rename(conversation_id, body.title)
        if body.starred is not None:
            store.set_starred(conversation_id, body.starred)
        if body.account_id is not None:
            store.set_scope(conversation_id, tenant_id=body.account_id)
        if body.archived is not None:
            # `archived: false` is RESTORE — the way back from a soft delete.
            store.set_archived(conversation_id, body.archived)
        meta = store.conversation(conversation_id, principal)
        _require_visible(meta, scope)
        return _conv(meta)

    @router.delete(subpath + "/conversations/{conversation_id}", tags=["chat"], status_code=204)
    def delete_conversation(request: Request, conversation_id: str):
        scope = site_scope.resolve()
        principal = _principal(request)
        store = _store()
        _require_visible(store.conversation(conversation_id, principal), scope)
        from axiom.governance import retention as gr

        # MVP-05-A3: a conversation is an operational record. Deleting it
        # archives it (the content stays, inside its floor, and "restore from
        # Settings" brings it back), and the deletion is recorded in the
        # tamper-evident chain first. There is no delete-forever here; one
        # would go through ``retention.check_deletion`` like the Library's.
        gr.record_deletion(
            kind="chat.conversation", subject=conversation_id, principal=principal,
            site=getattr(scope, "site", None) or None, retention_class=gr.RetentionClass.OPERATIONAL,
            forever=False,
        )
        store.archive(conversation_id)
        return Response(status_code=204)

    @router.post(subpath + "/conversations/{conversation_id}/messages", tags=["chat"])
    async def send_message(request: Request, conversation_id: str, body: SendBody):
        scope = site_scope.resolve()
        principal = _principal(request)
        store = _store()
        session = store.load(conversation_id, principal=principal)
        if session is None or not scope.permits(session.site_id or ""):
            # Uniform 404 (missing or out of scope) — appkit self-heals by
            # opening a fresh conversation and resending.
            raise HTTPException(404, "conversation not found")
        session.principal_id = principal or session.principal_id
        # Retrieval is scoped to the conversation's tenant, not to whichever
        # node served the request. An absent tenant is the single-tenant
        # install; it is never a wider read (see scope.shared_corpora_for).
        meta = store.conversation(conversation_id, principal)
        tenant_id = (meta or {}).get("tenant_id") or None
        return _stream_turn(
            session,
            body.content,
            conversation_id,
            store,
            persist=True,
            tenant_id=tenant_id,
            view_context=body.view_context,
        )

    @router.post(subpath + "/guest/message", tags=["chat"])
    async def guest_message(request: Request, body: GuestBody):
        # Guest history stays client-side (appkit keeps it local); the server
        # streams a reply from an ephemeral session it does not persist.
        from axiom.infra.orchestrator.session import Session

        session = Session()
        cid = body.conversation_id or session.session_id
        return _stream_turn(
            session,
            body.message,
            cid,
            _store(),
            persist=False,
            tenant_id=body.account_id or None,
        )

    @router.post(subpath + "/messages/feedback", tags=["chat"], status_code=204)
    def submit_feedback(request: Request, body: FeedbackBody):
        # message_id is "<session_id>:<seq>" (as get_detail emits it).
        sid, sep, seq_s = body.message_id.rpartition(":")
        if not sep or not seq_s.isdigit():
            raise HTTPException(404, "message not found")
        scope = site_scope.resolve()
        principal = _principal(request)
        store = _store()
        # You can only rate a conversation you can see (readable + in scope).
        _require_visible(store.conversation(sid, principal), scope)
        store.record_feedback(
            sid, int(seq_s), rating=body.rating, comment=body.comment or "", principal=principal
        )
        return Response(status_code=204)


__all__ = ["register_routes", "set_test_engine", "set_turn_runner", "reset_turn_runner"]
