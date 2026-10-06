# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``POST /gate/keys`` — a signed-in person mints their own key.

The invitation flow took the administrator out of *holding* somebody else's key.
It did not take them out of the loop: they still create an invitation and send a
code. The goal is that an administrator generates nothing at all.

They do not have to. By the time somebody wants a key they have already signed
in, and the gate knows who they are and what role they hold. The sign-in is the
proof of identity; the role somebody granted is the authorisation. A key is those
two facts written into a credential the command line can carry, so the person it
belongs to can mint it themselves.

The administrator's only act is the one that was always theirs: granting the
role. No secret is created by them, sent by them, or seen by them.

**Scopes come from the session, never from the request.** A caller choosing their
own scopes is a caller choosing their own authorisation, which is the thing a
role exists to prevent. The request body is read for nothing at all.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


def register_self_key_route(
    router: APIRouter,
    *,
    keys_path: Callable[[], Path | None],
    claims_of: Callable[[Request], dict | None],
) -> None:
    """Attach the self-service key route. Both seams are callables so the
    environment and the session are read per request, as the rest of this
    surface does."""

    @router.post("/gate/keys")
    async def mint_own_key(request: Request) -> JSONResponse:
        from axiom.webauth import ApiKeysFileError, append_api_key_record, mint_api_key

        claims = claims_of(request)
        if not claims:
            return _refuse(401, "sign in first", "no_session")

        handle = _principal_of(claims)
        if not handle:
            return _refuse(
                403,
                "this sign-in carries no name the node can use as a principal",
                "unnamed_session",
            )

        roles = tuple(str(r) for r in (claims.get("roles") or ()) if r)
        if not roles:
            return _refuse(
                403,
                "your account has no role yet, so a key would authorise nothing. "
                "Ask whoever administers this node to grant you one, then try again",
                "no_role",
            )

        from ..role_bundles import default_bundle_registry

        registry = default_bundle_registry()
        known = set(registry.roles())
        missing = [r for r in roles if r not in known]
        if missing:
            # Not ignored: dropping an unknown role would mint a narrower key
            # than the person was granted and leave them debugging a permission
            # they actually hold.
            return _refuse(
                403,
                f"your account holds the role(s) {', '.join(missing)}, which this "
                f"node does not define. Ask an administrator to fix the grant",
                "unknown_role",
            )

        scopes = tuple(registry.resolve(roles))
        if not scopes:
            return _refuse(
                403,
                f"the role(s) {', '.join(roles)} grant no scopes on this node, so a "
                f"key would authorise nothing",
                "empty_role",
            )

        path = keys_path()
        if path is None:
            log.warning("self-service key: no api-keys path configured")
            return _refuse(503, "this node does not serve API keys", "unavailable")

        token, record = mint_api_key(
            principal=handle,
            scopes=scopes,
            name=f"self-service, minted by the signed-in holder ({', '.join(roles)})",
            site=claims.get("site") or None,
        )
        try:
            append_api_key_record(path, record)
        except ApiKeysFileError as exc:
            log.error("self-service key: could not store: %s", exc)
            return _refuse(500, "the key could not be stored", "storage_failed")

        log.info(
            "self-service key %s minted for %s (%s)",
            record["key_id"],
            handle,
            ",".join(roles),
        )
        return JSONResponse(
            {
                # Once, to the person it belongs to. That is the entire point.
                "token": token,
                "key_id": record["key_id"],
                "principal": record["principal"],
                "scopes": list(record["scopes"]),
                "site": record["site"],
            }
        )


def _principal_of(claims: dict) -> str:
    """The handle this session names, by the same rule the rest of the gate uses.

    Falls back through the claims an identity provider may or may not send, and
    returns an empty string rather than inventing one: a key belonging to nobody
    would make the audit trail say so.
    """
    try:
        from axiom.infra.principal import principal_from_idp_subject

        handle = principal_from_idp_subject(
            str(claims.get("sub") or ""), str(claims.get("site") or "")
        )
        if handle:
            return str(handle)
    except Exception:  # noqa: BLE001 — fall through to the local-part rule
        pass
    email = str(claims.get("email") or "")
    local = email.split("@", 1)[0].strip()
    if not local:
        return ""
    site = str(claims.get("site") or "").strip()
    return f"@{local}:{site}" if site else f"@{local}"


def _refuse(status: int, detail: str, code: str) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code}, status_code=status)
