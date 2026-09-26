# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The gate's outbound email seam.

Account flows (reset, verification, magic link, changed-password notice)
send mail through ONE injectable sender. The default is the
:class:`FileOutboxSender` — a locked JSONL outbox on disk: real,
observable, and exactly what a dev/local node needs; a deployed site
wires an SMTP/notifications-channel sender through :func:`set_sender`
(the same pattern as the gate's user store).

The dev email log endpoint reads this outbox — and, like its
SoilMetrix ancestor, it is served ONLY when ``AXIOM_ENVIRONMENT`` is
``development`` or ``test``, because the outbox holds live reset /
verify / magic tokens.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from axiom.infra.state import locked_append_jsonl

OUTBOX_ENV = "AXIOM_GATE_OUTBOX_FILE"


@dataclass(frozen=True)
class OutboundEmail:
    to: str
    subject: str
    body: str
    kind: str  # reset | verify | magic_link | notice
    sent_at: str = ""

    def stamped(self) -> OutboundEmail:
        return OutboundEmail(
            to=self.to,
            subject=self.subject,
            body=self.body,
            kind=self.kind,
            sent_at=datetime.now(UTC).isoformat(),
        )


class EmailSender(Protocol):
    def send(self, mail: OutboundEmail) -> None: ...


class FileOutboxSender:
    """Append each mail to a locked JSONL outbox (fail loudly, never drop)."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        raw = str(path) if path else os.environ.get(OUTBOX_ENV, "")
        if not raw:
            raise ValueError(
                f"FileOutboxSender needs a path (or ${OUTBOX_ENV}): the outbox is "
                "the record that mail was sent — there is no silent default"
            )
        self._path = Path(raw)

    @property
    def path(self) -> Path:
        return self._path

    def send(self, mail: OutboundEmail) -> None:
        locked_append_jsonl(self._path, asdict(mail.stamped()))

    def sent(self) -> list[dict]:
        if not self._path.is_file():
            return []
        out = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out


_sender: EmailSender | None = None


def get_sender() -> EmailSender:
    """The process-wide sender. Defaults to the file outbox when the env
    names one; otherwise raises — a flow that needs email must not
    pretend to have sent it."""
    global _sender
    if _sender is None:
        _sender = FileOutboxSender()
    return _sender


def set_sender(sender: EmailSender) -> None:
    global _sender
    _sender = sender


def reset_sender() -> None:
    global _sender
    _sender = None


def dev_email_log_enabled() -> bool:
    """The SoilMetrix rule, ported: the log carries live tokens, so it is
    served only in development/test — never by default, never in staging."""
    return os.environ.get("AXIOM_ENVIRONMENT", "").lower() in {"development", "test"}


__all__ = [
    "OUTBOX_ENV",
    "EmailSender",
    "FileOutboxSender",
    "OutboundEmail",
    "dev_email_log_enabled",
    "get_sender",
    "reset_sender",
    "set_sender",
]
