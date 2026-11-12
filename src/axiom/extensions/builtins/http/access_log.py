# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Keep credentials out of the access log.

The access log records every request line, query string included. An OAuth
sign-in returns to ``/gate/oidc/callback?code=...&state=...``, so every sign-in
wrote an authorization code into the journal in plain text: seventeen of them
on one node in one week. A code is single-use and was already redeemed by the
time anyone could read it, so none was exploitable, but a log is the wrong
place for a credential of any lifetime, and the next parameter to land there
may live longer.

The filter rewrites the values of known credential parameters to ``<redacted>``
and keeps the parameter names, so the log still shows that a callback happened
and which fields it carried.
"""

from __future__ import annotations

import logging
import re

#: Query parameters whose values are credentials or bind one. Names are kept;
#: values are not.
SENSITIVE_PARAMS = (
    "code",
    "state",
    "session_state",
    "token",
    "access_token",
    "id_token",
    "refresh_token",
    "client_secret",
    "invite",
)

_PATTERN = re.compile(r"([?&](?:" + "|".join(SENSITIVE_PARAMS) + r")=)[^&\s\"]*", re.IGNORECASE)


def redact(text: str) -> str:
    """``text`` with every sensitive query value replaced."""
    return _PATTERN.sub(r"\1<redacted>", text)


class RedactQueryCredentials(logging.Filter):
    """Rewrites credential values in uvicorn's access record, never drops it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def install(logger_name: str = "uvicorn.access") -> None:
    """Attach the filter once; calling it again is a no-op."""
    logger = logging.getLogger(logger_name)
    if not any(isinstance(f, RedactQueryCredentials) for f in logger.filters):
        logger.addFilter(RedactQueryCredentials())


__all__ = ["SENSITIVE_PARAMS", "RedactQueryCredentials", "install", "redact"]
