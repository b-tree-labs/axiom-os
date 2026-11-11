# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Reply-bind-back correlation transport (ADR-067 §3).

Every outbound notification mints a correlation id and embeds it two ways
so a human reply can be tied back to the originating agent: a vendor-native
field (handled per adapter) and a forward-surviving **body token** in a
small footer. On inbound, the gateway recovers the token and looks up the
originating actor — making the ``Thread`` mapping load-bearing.

``ThreadStore`` here is the in-memory store, for tests and dev boxes with no
database. ``threads_db.DbThreadStore`` keeps the same bindings in the
``Thread`` table so a reply still binds back after a restart, and
``threads_db.default_thread_store()`` picks between them.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

# 8 hex chars → ~10^9.6 space over the 24h dedup window (ADR-067 §3).
_TOKEN_LEN = 8
_FOOTER = "[axi-corr: {token}]"
_TOKEN_RE = re.compile(rf"\[axi-corr:\s*([0-9a-f]{{{_TOKEN_LEN}}})\]")


def mint_correlation_id() -> str:
    return f"corr-{uuid.uuid4().hex}"


def body_token(correlation_id: str) -> str:
    """Deterministic short token derived from a correlation id."""
    digits = "".join(c for c in correlation_id if c in "0123456789abcdef")
    return (digits + "0" * _TOKEN_LEN)[:_TOKEN_LEN]


def embed_footer(text: str, correlation_id: str) -> str:
    """Append the forward-surviving correlation footer to outbound text."""
    return f"{text}\n\n{_FOOTER.format(token=body_token(correlation_id))}"


def parse_token(text: str) -> str | None:
    """Recover the correlation token from inbound (forwarded) text."""
    m = _TOKEN_RE.search(text or "")
    return m.group(1) if m else None


@dataclass(frozen=True)
class ThreadRecord:
    correlation_id: str
    token: str
    actor: str
    vendor: str
    thread_ref: str | None = None


class ThreadStore:
    durable = False

    def __init__(self) -> None:
        self._by_corr: dict[str, ThreadRecord] = {}
        self._by_token: dict[str, ThreadRecord] = {}
        self._claimed: dict[str, str] = {}

    def bind(
        self,
        correlation_id: str,
        *,
        actor: str,
        vendor: str,
        thread_ref: str | None = None,
        receipt_id: str | None = None,
    ) -> ThreadRecord:
        rec = ThreadRecord(
            correlation_id=correlation_id,
            token=body_token(correlation_id),
            actor=actor,
            vendor=vendor,
            thread_ref=thread_ref,
        )
        self._by_corr[correlation_id] = rec
        self._by_token[rec.token] = rec
        return rec

    def by_token(self, token: str) -> ThreadRecord | None:
        return self._by_token.get(token)

    def by_correlation(self, correlation_id: str) -> ThreadRecord | None:
        return self._by_corr.get(correlation_id)

    def claim(self, token: str) -> str | None:
        """Mark a bound token resolved. A claim id for the first claim, so a
        replayed or duplicated reply cannot act twice, and ``None`` for the
        rest. Only that id can reopen the token (:meth:`release`)."""
        rec = self._by_token.get(token)
        if rec is None or rec.correlation_id in self._claimed:
            return None
        claim_id = uuid.uuid4().hex
        self._claimed[rec.correlation_id] = claim_id
        return claim_id

    def release(self, token: str, claim_id: str | None) -> bool:
        """Reopen a token held by ``claim_id``, when the action it triggered
        failed. True if this reopened it. Anything else changes nothing, so a
        reply that lost the claim cannot reopen the winner's."""
        rec = self._by_token.get(token)
        if rec is None or claim_id is None or self._claimed.get(rec.correlation_id) != claim_id:
            return False
        del self._claimed[rec.correlation_id]
        return True


__all__ = [
    "mint_correlation_id",
    "body_token",
    "embed_footer",
    "parse_token",
    "ThreadRecord",
    "ThreadStore",
]
