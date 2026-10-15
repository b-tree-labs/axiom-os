# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Database-backed chat sessions — the store a second surface can reach.

The file store keeps a session as JSON under ``runtime/sessions/``, which is
the one shape that cannot support "one chat, many surfaces": every machine
has its own copy, so a conversation started on a laptop is invisible from a
phone or a web harness. Files stay for local testing; this is the store a
hosted session plane uses.

It satisfies the same contract as the file store, ownership boundary
included. A store that persists correctly and enforces ownership loosely
would be worse than files — files at least never left the machine.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.orchestrator.session import (
    Session,
    may_adopt,
    node_legacy_handles,
)


class DatabaseSessionStore:
    """Sessions in the extension's own schema, per ADR-052.

    Rows are written through ``session_for("chat")`` so the schema and
    ``search_path`` are the provider's business, never this module's. An
    ``engine`` may be passed for local testing; production takes the
    platform's.
    """

    def __init__(self, engine: Any | None = None) -> None:
        self._engine = engine

    # -- session plumbing ---------------------------------------------------

    def _session(self):
        from contextlib import contextmanager

        if self._engine is None:
            from axiom.infra.db import session_for

            return session_for("chat")

        @contextmanager
        def _local():
            from sqlalchemy.orm import Session as SASession

            with SASession(self._engine) as s:
                yield s

        return _local()

    @staticmethod
    def _readable_by(owner: str, principal: str) -> bool:
        """The same rule the file store applies, in one place.

        No principal means no check — the CLI's behaviour, where the only
        principal is the person at the keyboard. An unowned row predates
        ownership and stays readable. A legacy row is adopted only when the
        asker IS this installation's canonical human; deriving the handle set
        without also checking the asker is what made an earlier version leak.
        """
        if not principal or not owner or owner == principal:
            return True
        return owner in node_legacy_handles() and may_adopt(principal)

    # -- the SessionStore contract -----------------------------------------

    def create(
        self,
        context: dict | None = None,
        *,
        site_id: str = "",
        tenant_id: str = "",
        principal_id: str = "",
    ) -> Session:
        session = Session(
            context=context or {},
            site_id=site_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        # Persist the (empty) row now, unlike the file store: a shared surface
        # must be able to select and GET a conversation the instant it is
        # created, before any message — and the message-send path needs to
        # recover its scope by id. save() deliberately skips empty sessions
        # (the append path), so the insert is explicit here.
        from axiom.extensions.builtins.chat.db_models import ChatSession

        with self._session() as s:
            if s.get(ChatSession, session.session_id) is None:
                s.add(
                    ChatSession(
                        session_id=session.session_id,
                        principal_id=session.principal_id or "",
                        site_id=session.site_id or "",
                        tenant_id=session.tenant_id or "",
                        starred=bool(session.starred),
                        title=session.title or "",
                        payload={"context": session.context or {}},
                    )
                )
                s.commit()
        return session

    def save(self, session: Session) -> str | None:
        """Upsert the session, APPEND its new messages.

        Saving turn N costs one INSERT, not a rewrite of turns 1..N. Holding
        the conversation in the session's JSON made every turn rewrite the
        whole history — O(n) per turn, O(n-squared) over a session, measured
        at 108 KB per save by turn 200. Tolerable on local disk; not over a
        network.

        Empty sessions are skipped, as in the file store.
        """
        if not session.messages:
            return None
        from sqlalchemy import func, select

        from axiom.extensions.builtins.chat.db_models import ChatMessage, ChatSession

        # Serialise the session WITHOUT its messages.
        #
        # `to_dict()` renders every message and we then throw them away, which
        # is O(n) per turn in CPU even though the write is O(1) — it was still
        # visible in the measurement (4.1 ms at 50 turns rising to 6.8 at
        # 200). Messages are swapped out for the call so the payload is still
        # produced by `to_dict()` itself, and a field added to the dataclass
        # tomorrow is still carried without anyone remembering to add it here.
        # `_payload_matches_to_dict` in the tests pins that.
        held = session.messages
        session.messages = []
        try:
            payload = session.to_dict()
        finally:
            session.messages = held
        payload.pop("messages", None)

        with self._session() as s:
            row = s.get(ChatSession, session.session_id)
            if row is None:
                row = ChatSession(session_id=session.session_id)
                s.add(row)
                # Scope + star are set at creation and PRESERVED on every later
                # save, so an append-save from a stale in-memory Session (loaded
                # before another surface moved or starred it) can't revert the
                # column. set_scope / set_starred are the explicit mutators.
                row.site_id = session.site_id or ""
                row.tenant_id = session.tenant_id or ""
                row.starred = bool(session.starred)
            row.principal_id = session.principal_id or ""
            row.title = session.title or ""
            row.payload = payload

            # MAX(seq), not COUNT(*). Both answer "where do I append", but
            # count scans this session's index entries — O(n) in turns, which
            # is the cost this change exists to remove — while max reads the
            # (session_id, seq) index backwards in constant time.
            #
            # It is also the safer answer: a position is never REUSED, so if a
            # row were ever removed the next append still takes a fresh
            # position rather than colliding with a survivor.
            highest = s.execute(
                select(func.max(ChatMessage.seq)).where(
                    ChatMessage.session_id == session.session_id
                )
            ).scalar()
            stored = 0 if highest is None else highest + 1

            # Append only what is not already there. A resend of the same
            # turns is therefore idempotent rather than a duplicate.
            for position in range(stored, len(session.messages)):
                message = session.messages[position]
                s.add(
                    ChatMessage(
                        session_id=session.session_id,
                        seq=position,
                        role=message.role,
                        content=message.content or "",
                        tool_calls=message.tool_calls or None,
                        timestamp=message.timestamp or "",
                    )
                )
            s.commit()
        return session.session_id

    @staticmethod
    def _rebuild(db_session, row) -> Session:
        """Reassemble a Session from its row plus its messages.

        Ordered by `seq`, never by timestamp: surfaces run on different
        machines with disagreeing clocks, and two turns can share a
        millisecond.
        """
        from sqlalchemy import select

        from axiom.extensions.builtins.chat.db_models import ChatMessage

        payload = dict(row.payload or {})
        payload["session_id"] = row.session_id
        payload["messages"] = [
            {
                "role": m.role,
                "content": m.content,
                "timestamp": m.timestamp,
                **({"tool_calls": m.tool_calls} if m.tool_calls else {}),
            }
            for m in db_session.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == row.session_id)
                .order_by(ChatMessage.seq)
            ).scalars()
        ]
        return Session.from_dict(payload)

    def load(self, session_id: str, principal: str = "") -> Session | None:
        from axiom.extensions.builtins.chat.db_models import ChatSession

        with self._session() as s:
            row = s.get(ChatSession, session_id)
            if row is None:
                return None
            if not self._readable_by(row.principal_id or "", principal):
                return None
            session = self._rebuild(s, row)
            # Columns are authoritative for owner + scope axes + star. principal
            # in particular: a created-but-empty row has no principal in its
            # payload, so rebuilding from payload alone would drop the owner and
            # the next save() would persist an empty one.
            session.principal_id = row.principal_id or ""
            session.site_id = row.site_id or ""
            session.tenant_id = row.tenant_id or ""
            session.starred = bool(row.starred)
            return session

    def list_sessions(
        self, include_archived: bool = False, principal: str = ""
    ) -> list[str]:
        from sqlalchemy import select

        from axiom.extensions.builtins.chat.db_models import ChatSession

        with self._session() as s:
            stmt = select(ChatSession).order_by(ChatSession.updated_at.desc())
            if not include_archived:
                stmt = stmt.where(ChatSession.archived.is_(False))
            rows = s.execute(stmt).scalars().all()
        return [
            r.session_id
            for r in rows
            if self._readable_by(r.principal_id or "", principal)
        ]

    def latest_for(self, principal: str) -> Session | None:
        for session_id in self.list_sessions(principal=principal):
            session = self.load(session_id, principal=principal)
            if session is not None:
                return session
        return None

    def rename(self, session_id: str, title: str) -> bool:
        from axiom.extensions.builtins.chat.db_models import ChatSession

        with self._session() as s:
            row = s.get(ChatSession, session_id)
            if row is None:
                return False
            row.title = title
            payload = dict(row.payload)
            payload["title"] = title
            row.payload = payload
            s.commit()
        return True

    def archive(self, session_id: str) -> bool:
        from axiom.extensions.builtins.chat.db_models import ChatSession

        with self._session() as s:
            row = s.get(ChatSession, session_id)
            if row is None:
                return False
            row.archived = True
            s.commit()
        return True

    # -- scoped listing + web mutators (C2) --------------------------------

    def list_meta(
        self,
        *,
        site_id: str | None = None,
        tenant_id: str | None = None,
        principal: str = "",
        include_archived: bool = False,
        search: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Conversation metadata for a scope, most-recent-first.

        The projection the /api/v1/chat list reads from — id/title/starred/
        timestamps, never the messages. Filters are AND-ed: a ``None`` axis is
        unscoped (the CLI's whole-store view), a value scopes to it. Ownership
        still applies on top, so a tenant filter never widens what a principal
        may read.
        """
        from sqlalchemy import select

        from axiom.extensions.builtins.chat.db_models import ChatSession

        with self._session() as s:
            stmt = select(ChatSession).order_by(ChatSession.updated_at.desc())
            if not include_archived:
                stmt = stmt.where(ChatSession.archived.is_(False))
            if site_id is not None:
                stmt = stmt.where(ChatSession.site_id == site_id)
            if tenant_id is not None:
                stmt = stmt.where(ChatSession.tenant_id == tenant_id)
            if search:
                stmt = stmt.where(ChatSession.title.ilike(f"%{search}%"))
            # Ownership has to be part of the QUERY, not a filter over its page.
            # Limiting first pages the raw table: another principal's rows can
            # fill the page and then all be dropped here, so the asker's list
            # comes back short or empty while their conversations exist.
            if principal:
                from sqlalchemy import or_

                readable = [
                    ChatSession.principal_id.is_(None),
                    ChatSession.principal_id == "",
                    ChatSession.principal_id == principal,
                ]
                if may_adopt(principal):
                    legacy = [h for h in node_legacy_handles() if h]
                    if legacy:
                        readable.append(ChatSession.principal_id.in_(legacy))
                stmt = stmt.where(or_(*readable))
            if limit:
                stmt = stmt.limit(limit)
            rows = s.execute(stmt).scalars().all()
            # Kept as a backstop: the SQL predicate above mirrors _readable_by,
            # and this is what fails loudly if the two ever drift apart.
            return [
                self._row_to_meta(r)
                for r in rows
                if self._readable_by(r.principal_id or "", principal)
            ]

    @staticmethod
    def _row_to_meta(r) -> dict:
        """The conversation projection — id/title/starred/scope/timestamps."""
        return {
            "id": r.session_id,
            "title": r.title or "",
            "starred": bool(r.starred),
            "archived": bool(r.archived),
            "site_id": r.site_id or "",
            "tenant_id": r.tenant_id or "",
            "principal_id": r.principal_id or "",
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "updated_at": r.updated_at.isoformat() if r.updated_at else "",
        }

    def conversation(self, session_id: str, principal: str = "") -> dict | None:
        """One conversation's metadata (no messages), or None if missing/hidden."""
        from axiom.extensions.builtins.chat.db_models import ChatSession

        with self._session() as s:
            row = s.get(ChatSession, session_id)
            if row is None or not self._readable_by(row.principal_id or "", principal):
                return None
            return self._row_to_meta(row)

    def get_detail(
        self,
        session_id: str,
        *,
        principal: str = "",
        message_limit: int | None = None,
        messages_before: str | None = None,
    ) -> dict | None:
        """Conversation + its messages, the shape the /api/v1/chat detail returns.

        Ordered by ``seq`` (never timestamp — clocks disagree across surfaces).
        ``message_limit`` returns the newest N with ``messages_has_more`` set;
        ``messages_before`` (an ISO ``created_at`` cursor) pages older, for the
        client's load-earlier. ``system`` turns are internal and omitted.
        """
        from sqlalchemy import select

        from axiom.extensions.builtins.chat.db_models import ChatMessage, ChatSession

        with self._session() as s:
            row = s.get(ChatSession, session_id)
            if row is None or not self._readable_by(row.principal_id or "", principal):
                return None
            conv = self._row_to_meta(row)
            rows = list(
                s.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.seq)
                ).scalars()
            )
            visible = [m for m in rows if m.role != "system"]
            total = len(visible)
            if messages_before:
                visible = [m for m in visible if (m.timestamp or "") < messages_before]
            has_more = False
            if message_limit is not None and len(visible) > message_limit:
                visible = visible[-message_limit:]
                has_more = True
            messages = [
                {
                    "id": f"{m.session_id}:{m.seq}",
                    "role": m.role,
                    "content": m.content or "",
                    "created_at": m.timestamp or "",
                    **({"tool_calls": m.tool_calls} if m.tool_calls else {}),
                }
                for m in visible
            ]
            return {
                "conversation": conv,
                "messages": messages,
                "message_count": total,
                "messages_has_more": has_more,
                "oldest_message_created_at": messages[0]["created_at"] if messages else None,
            }

    def set_starred(self, session_id: str, starred: bool) -> bool:
        from axiom.extensions.builtins.chat.db_models import ChatSession

        with self._session() as s:
            row = s.get(ChatSession, session_id)
            if row is None:
                return False
            row.starred = bool(starred)
            s.commit()
        return True

    def record_feedback(
        self,
        session_id: str,
        seq: int,
        *,
        rating: str,
        comment: str = "",
        principal: str = "",
    ) -> bool:
        """Upsert one principal's feedback on one message (training signal).

        Kept in its own table, not on the append-only message row: a rating can
        change and is per-principal. One row per (message, principal), so
        re-rating updates rather than duplicates.
        """
        from axiom.extensions.builtins.chat.db_models import ChatMessageFeedback

        with self._session() as s:
            row = s.get(ChatMessageFeedback, (session_id, seq, principal or ""))
            if row is None:
                row = ChatMessageFeedback(
                    session_id=session_id, seq=seq, principal_id=principal or ""
                )
                s.add(row)
            row.rating = rating or ""
            row.comment = comment or ""
            s.commit()
        return True

    def set_scope(
        self,
        session_id: str,
        *,
        tenant_id: str | None = None,
        site_id: str | None = None,
    ) -> bool:
        """Move a conversation to another tenant/site (the "move" action)."""
        from axiom.extensions.builtins.chat.db_models import ChatSession

        with self._session() as s:
            row = s.get(ChatSession, session_id)
            if row is None:
                return False
            if tenant_id is not None:
                row.tenant_id = tenant_id
            if site_id is not None:
                row.site_id = site_id
            s.commit()
        return True


__all__ = ["DatabaseSessionStore"]
