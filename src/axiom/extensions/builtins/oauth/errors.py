# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RFC 6749 §5.2 error surface for the token endpoint.

One exception type carries the OAuth error code, a human description, the HTTP
status, and any auth-challenge headers. Handlers raise it anywhere in the grant
pipeline; the router renders it as the spec's JSON body with ``Cache-Control:
no-store``. Keeping this as data (not scattered ``JSONResponse`` calls) means the
error contract has exactly one shape.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

# Error codes we emit (RFC 6749 §4.1.2.1 + §5.2, RFC 8707 §2 for invalid_target).
INVALID_REQUEST = "invalid_request"
INVALID_CLIENT = "invalid_client"
INVALID_GRANT = "invalid_grant"
UNAUTHORIZED_CLIENT = "unauthorized_client"
UNSUPPORTED_GRANT_TYPE = "unsupported_grant_type"
UNSUPPORTED_RESPONSE_TYPE = "unsupported_response_type"
ACCESS_DENIED = "access_denied"
INVALID_SCOPE = "invalid_scope"
INVALID_TARGET = "invalid_target"


class OAuthError(Exception):
    """A token-endpoint error rendered per RFC 6749 §5.2.

    ``invalid_client`` is a 401 (authentication failed) and carries a
    ``WWW-Authenticate`` challenge; the rest default to 400.
    """

    def __init__(
        self,
        error: str,
        description: str | None = None,
        *,
        status: int = 400,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description
        self.status = status
        self.headers = headers or {}

    def to_response(self) -> JSONResponse:
        body: dict[str, str] = {"error": self.error}
        if self.description:
            body["error_description"] = self.description
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache", **self.headers}
        return JSONResponse(body, status_code=self.status, headers=headers)


def invalid_client(description: str) -> OAuthError:
    """A 401 invalid_client with the Basic auth challenge (RFC 6749 §5.2)."""
    return OAuthError(
        INVALID_CLIENT,
        description,
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="oauth", charset="UTF-8"'},
    )


__all__ = [
    "ACCESS_DENIED",
    "INVALID_CLIENT",
    "INVALID_GRANT",
    "INVALID_REQUEST",
    "INVALID_SCOPE",
    "INVALID_TARGET",
    "UNAUTHORIZED_CLIENT",
    "UNSUPPORTED_GRANT_TYPE",
    "UNSUPPORTED_RESPONSE_TYPE",
    "OAuthError",
    "invalid_client",
]
