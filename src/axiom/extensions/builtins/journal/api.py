# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/journal``: the rows a signed-in person may see, the lanes, and a live stream.

GET only; a test walks the router. Every route needs a gate session; the person is named by
the platform's handle builder and their site scope is the session's site narrowed by what this
node serves, exactly as the attest routes scope reads. The stream (ADR-147 pattern; a named
``heartbeat`` event beside the comment since unit J2, and one recorded read per opening) sends one
event per row appended, ``id`` = the row's sequence number; a client reconnecting with
``Last-Event-ID`` is first sent every row after it from the store.

Registering the routes also starts ingest in the serving process (the live subscription and
the catch-up ticker), unless ``AXIOM_JOURNAL_INGEST=0``.

No ``from __future__ import annotations`` here: FastAPI must resolve the locally imported
``Request`` annotation at definition time.
"""

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from . import follow, ingest, rights, service, writer

HEARTBEAT_SECONDS = 10.0
INGEST_ENV = "AXIOM_JOURNAL_INGEST"

log = logging.getLogger("axiom.journal.api")


def _person(request) -> dict[str, Any] | None:
    """``{handle, site}`` for the gate session, or None."""
    from axiom.extensions.builtins.fleet.api import _session_resolver_factory
    from axiom.infra.principal import principal_from_idp_subject

    resolver = _session_resolver_factory()
    credential = resolver(request) if resolver is not None else None
    claims = getattr(credential, "claims", None) or {}
    sub = claims.get("sub") or claims.get("email")
    if not sub:
        return None
    site = str(claims.get("site") or "")
    try:
        handle = principal_from_idp_subject(str(sub), site)
    except ValueError:
        return None
    return {"handle": handle, "site": site or None}


def _sse(event_id: int | None, event: str, data: dict[str, Any]) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _state_dir():
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def _record_stream_read(
    person: dict[str, Any], *, lanes: list[str], subject: str | None, after: int | None
) -> None:
    """Opening the stream is one read of the journal, recorded once (unit J2, the X2 rule)."""
    follow.record_read(handle=person["handle"], site=person.get("site"), lanes=lanes,
                       subject=subject, after=after, surface="web", state_dir=_state_dir())  # fmt: skip


def _start_ingest() -> None:
    if os.environ.get(INGEST_ENV, "1") == "0":
        return
    try:
        ingest.start()
    except Exception:  # noqa: BLE001 - the API serves whether or not ingest could start
        log.warning("journal ingest did not start; rows arrive only by catch-up", exc_info=True)


def register_routes(router: Any, *, subpath: str = "/journal") -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse, StreamingResponse

    _start_ingest()

    def refuse(status: int, detail: str, code: str = "refused") -> JSONResponse:
        return JSONResponse({"detail": detail, "code": code}, status_code=status)

    def who(request: Request):
        person = _person(request)
        if person is None:
            return refuse(401, "sign in first", "no_session")
        return rights.caller_from_session(person)

    @router.get(subpath, tags=["journal"])
    async def list_rows(
        request: Request,
        lanes: str | None = None,
        subject: str | None = None,
        subjects: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        mine: str | None = None,
        agent: str | None = None,
        levels: str | None = None,
    ):
        caller = who(request)
        if isinstance(caller, JSONResponse):
            return caller
        try:
            return service.query(caller, lanes=lanes, subject=subject, subjects=subjects,
                                 since=since, until=until, limit=limit, cursor=cursor,
                                 mine=mine, agent=agent, levels=levels)  # fmt: skip
        except ValueError as exc:
            return refuse(400, str(exc), "bad_request")

    @router.get(subpath + "/lanes", tags=["journal"])
    async def list_lanes(request: Request):
        caller = who(request)
        if isinstance(caller, JSONResponse):
            return caller
        return service.lanes_report(caller)

    @router.get(subpath + "/stream", tags=["journal"])
    async def stream(request: Request, lanes: str | None = None, subject: str | None = None,
                     mine: str | None = None, agent: str | None = None,
                     levels: str | None = None):  # fmt: skip
        caller = who(request)
        if isinstance(caller, JSONResponse):
            return caller
        last = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
        try:
            after = int(last) if last else None
        except ValueError:
            after = None
        once = request.query_params.get("once") in ("1", "true")
        narrow_args = {"mine": mine or False, "agent": agent, "levels": levels}
        try:
            wanted = set(follow.subscribable(caller, lanes))
            narrow = service.RowFilter.of(caller=caller, **narrow_args)
        except ValueError as exc:
            return refuse(400, str(exc), "bad_request")
        if not wanted:
            # Access at subscribe time (LB-J3-A7): nothing else, not the lane, not a count.
            return refuse(403, follow.NOT_AVAILABLE, "not_available")
        person = _person(request) or {"handle": caller.handle, "site": None}
        _record_stream_read(person, lanes=sorted(wanted), subject=subject, after=after)

        def matches(row: dict[str, Any]) -> bool:
            if row["lane"] not in wanted:
                return False
            if subject and not (
                row["subject"] == subject or row["subject"].startswith(subject + ".")
            ):
                return False
            return narrow.matches(row) and rights.visible(row, caller)[0]

        async def events():
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue(maxsize=follow.QUEUE_MAX)
            overflow = {"hit": False}

            def put(row: dict[str, Any]) -> None:
                try:
                    queue.put_nowait(row)
                except asyncio.QueueFull:
                    # Backpressure (LB-J3-A9): drop from the queue, never from the reader; the
                    # generator resyncs from the store from the last row it sent.
                    overflow["hit"] = True

            def on_row(row: dict[str, Any]) -> None:
                loop.call_soon_threadsafe(put, row)

            def drain() -> None:
                while not queue.empty():
                    queue.get_nowait()

            off = writer.on_append(on_row)
            try:
                # Live from now: a resync after an overflow starts at the newest row at opening,
                # never at the beginning of the journal.
                sent = (
                    after
                    if after is not None
                    else follow.newest_seq(caller, sorted(wanted), subject)
                )
                yield _sse(None, "ready", {"after": after, "lanes": sorted(wanted)})
                if after is not None:
                    for kind, item in follow.replay(caller, after=after, lanes=sorted(wanted),
                                                    subject=subject, narrow=narrow_args):  # fmt: skip
                        if kind == "gap":
                            yield _sse(None, "gap", item)
                        else:
                            sent = item["seq"]
                            yield _sse(item["seq"], "row", item)
                if once:
                    return
                last_beat = time.monotonic()
                while True:
                    if await request.is_disconnected():
                        return
                    if overflow["hit"]:
                        overflow["hit"] = False
                        drain()
                        for kind, item in follow.replay(caller, after=sent, lanes=sorted(wanted),
                                                        subject=subject, narrow=narrow_args):  # fmt: skip
                            if kind == "gap":
                                yield _sse(None, "gap", item)
                            else:
                                sent = item["seq"]
                                yield _sse(item["seq"], "row", item)
                        continue
                    try:
                        row = await asyncio.wait_for(
                            queue.get(), timeout=min(1.0, HEARTBEAT_SECONDS)
                        )
                    except TimeoutError:
                        if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                            last_beat = time.monotonic()
                            # The comment keeps proxies from closing an idle connection; the named
                            # event is what a browser sees, so the Timeline and the Tail block can
                            # tell a silent link from a quiet lane (W4's rule). No id: it never
                            # moves the resume point.
                            yield ": heartbeat\n\n"
                            yield _sse(None, "heartbeat", {"at": datetime.now(UTC).isoformat()})
                        continue
                    if row["seq"] <= sent or not matches(row):
                        continue
                    sent = row["seq"]
                    yield _sse(row["seq"], "row", follow.shown(row))
            finally:
                off()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


__all__ = ["HEARTBEAT_SECONDS", "INGEST_ENV", "register_routes"]
