# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``POST /gate/redeem`` — a colleague redeems from their own machine.

The invitation primitive took the administrator out of holding other people's
keys. It did not take them out of the loop: redemption ran on the node, and the
people being onboarded are exactly the people with no node access — developers
here get no SSH and no database credentials by design (ADR-002).

So the gate serves it. The code is presented, the key comes back once, to the
holder.

**This route mints a credential for whoever presents a valid code, so the code is
the authentication.** That is sound only because of properties the primitive
enforces rather than this route re-implementing them: single use, an expiry, and a
grant that can only shrink. What this module owns is the HTTP shape — a clean
refusal rather than a 500 for the malformed bodies an unauthenticated route
receives as a matter of course, and silence about both secrets.

Nothing here logs the code or the token. The route exists because a secret in a
transcript is the exposure; a secret in the node's log is the same thing with a
longer retention.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


def register_redeem_route(
    router: APIRouter,
    *,
    invitations_path: Callable[[], Path | None],
    keys_path: Callable[[], Path | None],
) -> None:
    """Attach the redemption route. Paths are callables so the environment is
    read per request, which is what the rest of this surface does and what lets a
    test point them somewhere else."""

    @router.post("/gate/redeem")
    async def redeem(request: Request) -> JSONResponse:
        from axiom.webauth import ApiKeysFileError, append_api_key_record
        from axiom.webauth.invitations import (
            InvitationRefused,
            InvitationsFileError,
            redeem_invitation,
        )

        try:
            body: Any = await request.json()
        except Exception:  # noqa: BLE001 — an unauthenticated route gets junk
            return _refuse(400, "send a JSON object with a `code`", "bad_request")
        if not isinstance(body, dict):
            return _refuse(400, "send a JSON object with a `code`", "bad_request")

        code = str(body.get("code") or "").strip()
        if not code:
            return _refuse(400, "no `code` in the request body", "bad_request")

        invites = invitations_path()
        keys = keys_path()
        if invites is None or keys is None:
            # A node that cannot say where either file lives cannot serve this.
            # Said as a server fault, because it is one.
            log.warning("redeem: no invitations or api-keys path configured")
            return _refuse(503, "this node does not serve invitation redemption", "unavailable")

        from ..role_bundles import default_bundle_registry

        registry = default_bundle_registry()

        def _resolve(names):
            """Resolve role names now, raising KeyError for one that is gone.

            The registry returns a union and says nothing about a name it did not
            recognise, which would let a vanished role read as a narrowing rather
            than an error.
            """
            known = set(registry.roles())
            for name in names:
                if name not in known:
                    raise KeyError(name)
            return registry.resolve(tuple(names))

        try:
            token, key_record, invitation = redeem_invitation(
                invites, code, resolve_roles=_resolve
            )
        except InvitationRefused as exc:
            # The reason, not a uniform refusal. A colleague who cannot tell
            # "already used" from "expired" has to go and ask somebody, which is
            # what this surface exists to stop. Nothing of the code is echoed.
            return _refuse(403, str(exc), "invitation_refused")
        except InvitationsFileError as exc:
            log.error("redeem: invitations file unreadable: %s", exc)
            return _refuse(503, "this node cannot read its invitations", "unavailable")

        try:
            append_api_key_record(keys, key_record)
        except ApiKeysFileError as exc:
            # The invitation is already spent and cannot be replayed, so say so
            # plainly rather than implying a retry will work.
            log.error("redeem: key could not be stored: %s", exc)
            return _refuse(
                500,
                "the invitation was spent but the key could not be stored. "
                "Ask for a new invitation — this one cannot be redeemed again",
                "storage_failed",
            )

        log.info(
            "redeem: issued key %s to %s (invitation %s)",
            key_record["key_id"],
            key_record["principal"],
            invitation["invitation_id"],
        )
        return JSONResponse(
            {
                # Once, to the holder. This is the only place it exists in
                # plaintext, which is the entire point of the route.
                "token": token,
                "key_id": key_record["key_id"],
                "principal": key_record["principal"],
                "scopes": list(key_record["scopes"]),
                "site": key_record["site"],
                "invited_by": invitation.get("invited_by") or "",
            }
        )


def _refuse(status: int, detail: str, code: str) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code}, status_code=status)
