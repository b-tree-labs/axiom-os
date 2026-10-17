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

    @router.get("/gate/keys")
    async def key_page(request: Request):
        """The door people actually use: a signed-in person presses one button.

        The key is minted only when they press it, shown once beside the command
        that stores it, and never placed in the page before then.
        """
        from fastapi.responses import HTMLResponse, RedirectResponse

        claims = claims_of(request)
        if not claims:
            return RedirectResponse("/gate/login?next=/gate/keys", status_code=303)
        handle = _principal_of(claims) or "(unnamed)"
        roles = tuple(str(r) for r in (claims.get("roles") or ()) if r)
        if not roles:
            return HTMLResponse(
                _page(
                    handle,
                    "",
                    "Your account has no role yet, so a key would allow nothing. "
                    "Ask whoever runs this site to grant you one, then come back.",
                )
            )
        from ..role_bundles import default_bundle_registry

        registry = default_bundle_registry()
        missing = [r for r in roles if r not in set(registry.roles())]
        if missing:
            return HTMLResponse(
                _page(
                    handle,
                    "",
                    f"Your role ({', '.join(missing)}) is not defined on this "
                    "site. Ask whoever runs it to fix the grant.",
                )
            )
        scopes = ", ".join(sorted(registry.resolve(roles)))
        host = (request.url.hostname or "site").split(".")[0]
        return HTMLResponse(_page(handle, scopes, "", store_name=f"{host}-key"))

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


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:  # noqa: BLE001 - a page must render without branding
        return "axi"


_VERBS = {"read": "read", "invoke": "use", "govern": "administer", "access": "reach"}


def _plain(scope: str) -> str:
    """A permission as a sentence: ``chat:invoke`` -> "use chat"."""
    mount, _, verb = scope.partition(":")
    action = _VERBS.get(verb or "", "do anything with")
    what = "everything this site serves" if mount == "*" else mount.replace("_", " ")
    if scope == "*":
        return "do anything on this site"
    return f"{action} {what}"


def _page(handle: str, scopes: str, problem: str, *, store_name: str = "site-key") -> str:
    """The key page. Plain HTML, no external assets, readable in light and dark."""
    import html

    cli = html.escape(_cli_name())
    who = html.escape(handle)
    body = (
        f"<p class=muted>Signed in as <b>{who}</b></p><p>{html.escape(problem)}</p>"
        if problem
        else f"""<p class=muted>Signed in as <b>{who}</b></p>
<p>A key lets your tools (the command line, an agent) act as you. Yours will let them:</p>
<ul>{"".join(f"<li>{html.escape(_plain(sc))}</li>" for sc in scopes.split(", ") if sc)}</ul>
<p class=muted><small>Exact permissions: <code>{html.escape(scopes)}</code></small></p>
<button id=mint>Create a key</button>
<div id=out hidden>
  <p><b>Store it now.</b> It is shown once. Run this, then paste the key when asked
  (what you paste stays hidden):</p>
  <pre>{cli} secrets set {html.escape(store_name)}</pre>
  <p>Your key:</p><pre id=token></pre>
  <button id=copy>Copy key</button>
</div>
<p id=err class=err hidden></p>
<script>
document.getElementById("mint").onclick = async (e) => {{
  e.target.disabled = true;
  const r = await fetch("/gate/keys", {{method: "POST", credentials: "same-origin"}});
  const d = await r.json();
  if (!r.ok) {{ const el = document.getElementById("err"); el.textContent = d.detail || "could not create a key"; el.hidden = false; return; }}
  document.getElementById("token").textContent = d.token;
  document.getElementById("out").hidden = false;
  e.target.hidden = true;
}};
document.getElementById("copy").onclick = () => navigator.clipboard.writeText(document.getElementById("token").textContent);
</script>"""
    )
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Your key</title>
<style>
:root{{color-scheme:light dark;--fg:#1b1b1b;--muted:#5b5b5b;--bg:#fff;--line:#d8d8d8}}
@media (prefers-color-scheme:dark){{:root{{--fg:#ececec;--muted:#a8a8a8;--bg:#161616;--line:#3a3a3a}}}}
body{{font:15px/1.5 system-ui,sans-serif;color:var(--fg);background:var(--bg);max-width:40rem;margin:3rem auto;padding:0 1rem}}
.muted{{color:var(--muted)}} code,pre{{font:13px ui-monospace,monospace}}
pre{{border:1px solid var(--line);border-radius:8px;padding:.6rem .8rem;white-space:pre-wrap;word-break:break-all}}
button{{font:inherit;padding:.5rem 1rem;border-radius:8px;border:1px solid var(--line);cursor:pointer}}
.err{{color:#c0392b}}
</style></head><body><h1>Your key</h1>{body}</body></html>"""


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
