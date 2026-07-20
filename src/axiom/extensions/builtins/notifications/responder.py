# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Vendor-agnostic conversational responder for text endpoints.

Every chat-ish inbound surface (Teams outgoing webhooks, Slack events,
Mattermost outgoing webhooks, SMS) has the same shape: a **sync window**
in which the vendor demands a reply, a **slow model** that can't always
make that window, and a **reply channel** for deferred answers. This core
encodes the contract a production deployment proved out:

- try the FAST model inside the sync budget → answer inline;
- otherwise return a rotating acknowledgement — and the ack is a PROMISE:
  the question is journaled to disk BEFORE the ack is returned, the slow
  model runs with a retry, failures produce an explicit message (never
  silence), delivery falls back to a secondary channel, and pending work
  survives restarts via :meth:`ConversationResponder.resume_pending`.

Endpoint glue stays thin: verify the vendor signature (gateway verifiers),
decode the payload (gateway decoders), then call
:meth:`ConversationResponder.handle` from a worker thread and return its
string inside the vendor's window.
"""

from __future__ import annotations

import json
import logging
import os
import random
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger("axiom.notifications.responder")

_ACKS = (
    "⏳ This one needs some thought — answer coming {where} shortly.",
    "⏳ Give me a minute on that; I'll post {where} when I have it.",
    "⏳ Working on it. Full answer lands {where} soon.",
    "⏳ Let me dig into that — back {where} in a bit.",
    "⏳ Good question; it needs the bigger model. Answer will show up {where}.",
    "⏳ On it — expect my answer {where} in a minute or two.",
)


@dataclass
class ResponderConfig:
    """Tunables; defaults match the Teams deployment that proved the shape."""

    sync_budget_s: float = 2.5
    slow_timeouts_s: tuple[float, ...] = (90.0, 45.0)
    pending_dir: Path = Path.home() / ".local/state/axiom/responder-pending"
    history_turns: int = 12
    where_deferred: str = "here"


@dataclass
class ConversationResponder:
    """The ack-is-a-promise conversation core.

    ``ask(question, history, timeout, fast)`` produces a completion —
    injectable, so any model stack (and any test) plugs in. ``reply(text)``
    delivers a deferred answer to the conversation; ``fallback_reply`` is
    the break-glass channel when ``reply`` fails.
    """

    ask: Callable[..., str]
    reply: Callable[[str], None]
    fallback_reply: Callable[[str], None] | None = None
    progress_reply: Callable[[str], None] | None = None  # optional "working" ping
    config: ResponderConfig = field(default_factory=ResponderConfig)
    _history: list[dict[str, str]] = field(default_factory=list)
    _pool: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=2)
    )

    # -- public surface ----------------------------------------------------

    def handle(self, question: str) -> str:
        """Blocking; call from a worker thread inside the vendor's window.

        Returns the inline reply: either the fast answer or an ack whose
        promise this object then keeps.
        """
        future = self._pool.submit(
            self.ask,
            question,
            history=list(self._history[-self.config.history_turns :]),
            timeout=self.config.sync_budget_s,
            fast=True,
        )
        try:
            answer = future.result(timeout=self.config.sync_budget_s)
            self._remember(question, answer)
            return answer
        except (FutureTimeout, Exception):  # noqa: BLE001 — defer, never fail
            qfile = self._enqueue(question)
            if self.progress_reply is not None:
                try:
                    self.progress_reply(f"⏳ working on: “{question[:90]}”")
                except Exception:  # noqa: BLE001 — progress is best-effort
                    _log.debug("progress ping failed", exc_info=True)
            self._pool.submit(self._finish, qfile, False)
            return random.choice(_ACKS).format(where=self.config.where_deferred)

    def resume_pending(self) -> int:
        """Finish (or explicitly apologize for) work orphaned by a restart."""
        try:
            leftovers = sorted(Path(self.config.pending_dir).glob("*.json"))
        except OSError:
            return 0
        for qfile in leftovers:
            self._pool.submit(self._finish, str(qfile), True)
        return len(leftovers)

    # -- internals -----------------------------------------------------------

    def _remember(self, question: str, answer: str) -> None:
        self._history.append({"role": "user", "content": question})
        self._history.append({"role": "assistant", "content": answer})
        del self._history[: -self.config.history_turns]

    def _enqueue(self, question: str) -> str:
        os.makedirs(self.config.pending_dir, exist_ok=True)
        path = Path(self.config.pending_dir) / f"{uuid.uuid4().hex}.json"
        path.write_text(json.dumps({"question": question}))
        return str(path)

    def _finish(self, qfile: str, resumed: bool) -> None:
        try:
            question = json.loads(Path(qfile).read_text())["question"]
        except Exception:  # noqa: BLE001 — unreadable journal entry
            _log.exception("unreadable pending file %s", qfile)
            Path(qfile).unlink(missing_ok=True)
            return

        answer = None
        for timeout in self.config.slow_timeouts_s:
            try:
                answer = self.ask(
                    question,
                    history=list(self._history[-self.config.history_turns :]),
                    timeout=timeout,
                    fast=False,
                )
                break
            except Exception:  # noqa: BLE001 — retry, then admit failure
                _log.exception("deferred answer attempt failed")
        if answer is None:
            answer = (
                f"I said I'd get back to you on “{question[:120]}” — "
                "but I hit an error answering it. Please ask again."
            )
        else:
            self._remember(question, answer)
        if resumed:
            answer = "(picking this back up after a restart) " + answer

        try:
            self.reply(answer)
        except Exception:  # noqa: BLE001 — primary channel down
            _log.exception("deferred reply failed; trying fallback")
            if self.fallback_reply is None:
                _log.error("no fallback reply channel — leaving %s queued", qfile)
                return
            try:
                self.fallback_reply(answer)
            except Exception:  # noqa: BLE001 — nothing deliverable; keep journal
                _log.exception("fallback reply failed — leaving %s queued", qfile)
                return
        Path(qfile).unlink(missing_ok=True)


__all__ = ["ConversationResponder", "ResponderConfig"]
