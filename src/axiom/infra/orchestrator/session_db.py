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

    def create(self, context: dict | None = None) -> Session:
        session = Session(context=context or {})
        self.save(session)
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
            return self._rebuild(s, row)

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


__all__ = ["DatabaseSessionStore"]
