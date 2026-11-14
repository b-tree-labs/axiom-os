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
role exists to prevent. The request body is read for one thing only: which of the
sites the account may use the key is for.

**Every key carries a site.** A key's site is what a data kit's tenant, a
site-held read and every per-site check reads; a key without one could do none of
that. The sites on offer are the node's home site (``AXIOM_SITE``), plus any the
account's roles list (``sites = [...]`` on a role in the roles file), and a
request for any other site is refused in words.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
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
        recourse = _support_href()
        roles = tuple(str(r) for r in (claims.get("roles") or ()) if r)
        if not roles:
            return HTMLResponse(
                _page(
                    handle,
                    "",
                    "Your account has no role yet, so a key would allow nothing. "
                    "A role is what an administrator grants you.",
                    recourse=recourse,
                    recourse_label="Request access",
                )
            )
        from ..role_bundles import default_bundle_registry, role_defined

        registry = default_bundle_registry()
        missing = [r for r in roles if not role_defined(registry, r)]
        if missing:
            return HTMLResponse(
                _page(
                    handle,
                    "",
                    f"Your role ({', '.join(missing)}) is not defined on this "
                    "site, so the grant needs fixing.",
                    recourse=recourse,
                    recourse_label="Get help",
                )
            )
        scopes = ", ".join(sorted(registry.resolve(roles)))
        host = (request.url.hostname or "site").split(".")[0]
        sites = allowed_sites(claims, registry, roles)
        if not sites:
            return HTMLResponse(_page(handle, "", _NO_HOME, recourse=recourse, recourse_label="Get help"))
        return HTMLResponse(_page(handle, scopes, "", store_name=f"{host}-key", sites=sites))

    @router.post("/gate/keys")
    async def mint_own_key(request: Request) -> JSONResponse:
        decision = self_service_decision(claims_of(request), await _requested_site(request))
        if isinstance(decision, Refusal):
            return _refuse(decision.status, decision.detail, decision.code)
        minted = mint_self_service_key(keys_path(), decision)
        if isinstance(minted, Refusal):
            return _refuse(minted.status, minted.detail, minted.code)
        # Once, to the person it belongs to. That is the entire point.
        return JSONResponse(minted)

    # ---- Account › Keys: a person's own keys (MVP-08-A5) -------------------

    @router.get("/gate/keys/mine")
    async def my_keys(request: Request) -> JSONResponse:
        """The caller's own keys: never a hash, never a token."""
        claims = claims_of(request)
        if not claims:
            return _refuse(401, "sign in first", "no_session")
        path = keys_path()
        if path is None:
            return JSONResponse({"keys": [], "role_scopes": []})
        return JSONResponse({
            "keys": [public_key_record(r) for r in own_key_records(path, claims)],
            "role_scopes": sorted(_role_scopes(claims)),
        })

    @router.post("/gate/keys/mine/{key_id}/rescope")
    async def rescope_my_key(key_id: str, request: Request) -> JSONResponse:
        """Narrow (or restore within your roles) one of your keys in place: same token."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - an empty body changes nothing
            body = {}
        add = [str(x) for x in (body.get("add_scope") or [])]
        remove = [str(x) for x in (body.get("remove_scope") or [])]
        return _own_key_action(request, key_id, "gate.rescope",
                               {"add_scope": add, "remove_scope": remove}, add=add)

    @router.post("/gate/keys/mine/{key_id}/revoke")
    async def revoke_my_key(key_id: str, request: Request) -> JSONResponse:
        return _own_key_action(request, key_id, "gate.revoke", {})

    def _own_key_action(request: Request, key_id: str, capability: str, extra: dict,
                        *, add: list[str] | None = None) -> JSONResponse:
        claims = claims_of(request)
        if not claims:
            return _refuse(401, "sign in first", "no_session")
        path = keys_path()
        if path is None:
            return _refuse(503, "this node does not serve API keys", "unavailable")
        mine = {r["key_id"] for r in own_key_records(path, claims)}
        if key_id not in mine:
            # The same answer for "not yours" and "no such key": nothing leaks.
            return _refuse(404, "no key of yours has that id", "not_yours")
        beyond = sorted(set(add or ()) - _role_scopes(claims))
        if beyond:
            return _refuse(403, f"your roles do not grant {', '.join(beyond)}, so a key of yours "
                                "cannot carry it", "beyond_role")
        result = _invoke_gate(capability, {"resource": "api-key", "key_id": key_id,
                                           "keys_file": str(path), **extra}, actor=_principal_of(claims))
        if not result.ok:
            return _refuse(400, "; ".join(result.errors), "refused")
        records = {r["key_id"]: r for r in own_key_records(path, claims, include_revoked=True)}
        return JSONResponse({"key": public_key_record(records.get(key_id, {"key_id": key_id})),
                             "same_token": True})


def own_key_records(path: Path, claims: dict, *, include_revoked: bool = False) -> list[dict]:
    """The key records whose principal is this session's person (with or without the site)."""
    from axiom.webauth import load_api_key_records

    handle = _principal_of(claims)
    if not handle:
        return []
    names = {handle, handle.split(":", 1)[0]}
    try:
        records = load_api_key_records(path)
    except Exception:  # noqa: BLE001 - an unreadable file lists nothing
        return []
    return [r for r in records
            if r.get("principal") in names and (include_revoked or not r.get("revoked_at"))]


def public_key_record(record: dict) -> dict:
    """What a key's owner sees: never its hash, never a token."""
    return {k: record.get(k) for k in ("key_id", "name", "site", "scopes", "created_at",
                                       "expires_at", "revoked_at")}


def _role_scopes(claims: dict) -> set[str]:
    try:
        from ..role_bundles import default_bundle_registry

        roles = tuple(str(r) for r in (claims.get("roles") or ()) if r)
        return set(default_bundle_registry().resolve(roles)) if roles else set()
    except Exception:  # noqa: BLE001 - no roles resolvable grants nothing
        return set()


def _invoke_gate(capability: str, params: dict, *, actor: str):
    """A gate verb through the dispatch chokepoint, as the caller (one audit row)."""
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    from .. import skills as gate_skills

    registry = SkillRegistry()
    gate_skills.bind(registry)
    ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(),
                       logger=logging.getLogger("axi.gate"), actor=actor or None)
    return invoke_capability(registry, capability, params, ctx, surface=WEB_SURFACE)


@dataclass(frozen=True)
class Refusal:
    """Why a key is not minted: the HTTP status, the person's sentence, a stable code."""

    status: int
    detail: str
    code: str


@dataclass(frozen=True)
class SelfServiceGrant:
    """What a signed-in person may mint: decided from the session, never the request."""

    handle: str
    roles: tuple[str, ...]
    scopes: tuple[str, ...]
    site: str


def self_service_decision(claims: dict | None, wanted_site: str = "") -> SelfServiceGrant | Refusal:
    """Decide what key this session may mint, or why it may not.

    One function for every door that mints a self-service key (the Keys page and
    the command line's browser sign-in), so the two cannot come to disagree about
    who may have what. Scopes come from the session's roles; the only thing a
    caller chooses is which of the allowed sites the key is for.
    """
    if not claims:
        return Refusal(401, "sign in first", "no_session")

    handle = _principal_of(claims)
    if not handle:
        return Refusal(
            403,
            "this sign-in carries no name the node can use as a principal",
            "unnamed_session",
        )

    where = _support_contact_sentence()
    roles = tuple(str(r) for r in (claims.get("roles") or ()) if r)
    if not roles:
        return Refusal(
            403,
            "your account has no role yet, so a key would authorise nothing. "
            "A role is what an administrator grants you." + where,
            "no_role",
        )

    from ..role_bundles import default_bundle_registry, role_defined

    registry = default_bundle_registry()
    missing = [r for r in roles if not role_defined(registry, r)]
    if missing:
        # Not ignored: dropping an unknown role would mint a narrower key
        # than the person was granted and leave them debugging a permission
        # they actually hold.
        return Refusal(
            403,
            f"your account holds the role(s) {', '.join(missing)}, which this "
            f"node does not define, so the grant needs fixing." + where,
            "unknown_role",
        )

    scopes = tuple(registry.resolve(roles))
    if not scopes:
        return Refusal(
            403,
            f"the role(s) {', '.join(roles)} grant no scopes on this node, so a "
            f"key would authorise nothing",
            "empty_role",
        )

    sites = allowed_sites(claims, registry, roles)
    if not sites:
        return Refusal(503, _NO_HOME, "no_home_site")
    site = (wanted_site or "").strip() or sites[0]
    if site not in sites:
        return Refusal(
            403,
            f"your account may mint a key for {', '.join(sites)}, not for {site!r}. "
            f"A key for another site is granted through your role." + where,
            "site_not_allowed",
        )
    return SelfServiceGrant(handle=handle, roles=roles, scopes=scopes, site=site)


def mint_self_service_key(path: Path | None, grant: SelfServiceGrant) -> dict | Refusal:
    """Mint the key a decision allows and record it. Returns the one-time reply body.

    The reply carries the token: it is shown once, to its holder, and never logged.
    """
    from axiom.webauth import ApiKeysFileError, append_api_key_record, mint_api_key

    if path is None:
        log.warning("self-service key: no api-keys path configured")
        return Refusal(503, "this node does not serve API keys", "unavailable")
    try:
        token, record = mint_api_key(
            principal=grant.handle.split(":", 1)[0],  # the person, held to the chosen site
            scopes=grant.scopes,
            name=f"self-service, minted by the signed-in holder ({', '.join(grant.roles)})",
            site=grant.site,
        )
    except ValueError as exc:
        return Refusal(400, str(exc), "bad_site")
    try:
        append_api_key_record(path, record)
    except ApiKeysFileError as exc:
        log.error("self-service key: could not store: %s", exc)
        return Refusal(500, "the key could not be stored", "storage_failed")

    log.info(
        "self-service key %s minted for %s (%s)",
        record["key_id"],
        grant.handle,
        ",".join(grant.roles),
    )
    _audit_self_mint(grant, record)
    return {
        "token": token,
        "key_id": record["key_id"],
        "principal": record["principal"],
        "scopes": list(record["scopes"]),
        "site": record["site"],
    }


def _audit_self_mint(grant: SelfServiceGrant, record: dict) -> None:
    """One action-chain row for a key minted at this door (MVP-13-A1). Never raises."""
    try:
        from axiom.infra import audit_trail
        from axiom.infra.paths import get_user_state_dir

        audit_trail.note_change(kind="api-key", subject=f"{record['key_id']} ({record['principal']})",
                                old=None, new={"scopes": list(record["scopes"]), "site": record.get("site")})
        audit_trail.record_invocation(
            state_dir=get_user_state_dir(), skill="gate.issue",
            params={"principal": record["principal"], "site": record.get("site"), "self_service": True},
            principal=grant.handle, site=record.get("site"), outcome="ok", errors_count=0,
            change=audit_trail.take_change(),
        )
    except Exception as exc:  # noqa: BLE001 - the audit never fails the mint
        log.warning("self-service key: audit write failed: %s", exc)


_NO_HOME = ("this node has no home site set (AXIOM_SITE), and a key must carry a site, so no "
            "key can be minted here until an administrator sets it")


def home_site(claims: dict | None = None) -> str:
    """The node's home site: ``AXIOM_SITE``, else the site the session names."""
    return (os.environ.get("AXIOM_SITE") or str((claims or {}).get("site") or "")).strip()


def allowed_sites(claims: dict, registry, roles) -> list[str]:
    """The sites this account may mint a key for: home first, then each its roles list."""
    out = [home_site(claims)] if home_site(claims) else []
    for site in registry.sites_for(roles):
        if site not in out:
            out.append(site)
    return out


async def _requested_site(request: Request) -> str:
    """The one thing read from a request: which site the key is for (query or JSON body)."""
    wanted = str(request.query_params.get("site") or "").strip()
    if wanted:
        return wanted
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - no body, or not JSON: the default site
        return ""
    return str(body.get("site") or "").strip() if isinstance(body, dict) else ""


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:  # noqa: BLE001 - a page must render without branding
        return "axi"


def _support_href() -> str:
    """The site's access recourse as a safe href, or "" when none is set.

    Best-effort: a page must render even if branding cannot be resolved, so any
    failure is "no recourse configured", not an error.
    """
    try:
        from axiom.infra.web_brand import resolve_web_brand, support_href

        return support_href(resolve_web_brand().support_contact)
    except Exception:  # noqa: BLE001 - the page renders without recourse
        return ""


def _support_contact_sentence() -> str:
    """A trailing sentence pointing at the site's recourse for the JSON path,
    or "" when none is configured. Shown only when the raw value is a safe
    contact (the same check the page link uses), so a misconfig adds nothing."""
    try:
        from axiom.infra.web_brand import resolve_web_brand, support_href

        raw = resolve_web_brand().support_contact.strip()
        return f" Request access: {raw}" if raw and support_href(raw) else ""
    except Exception:  # noqa: BLE001
        return ""


_VERBS = {"read": "read", "invoke": "use", "govern": "administer", "access": "reach"}


def _plain(scope: str) -> str:
    """A permission as a sentence: ``chat:invoke`` -> "use chat"."""
    mount, _, verb = scope.partition(":")
    action = _VERBS.get(verb or "", "do anything with")
    what = "everything this site serves" if mount == "*" else mount.replace("_", " ")
    if scope == "*":
        return "do anything on this site"
    return f"{action} {what}"


def _page(
    handle: str,
    scopes: str,
    problem: str,
    *,
    store_name: str = "site-key",
    recourse: str = "",
    recourse_label: str = "Request access",
    sites: list[str] | None = None,
) -> str:
    """The key page. Plain HTML, no external assets, readable in light and dark.

    ``recourse`` is a pre-sanitised href (see ``web_brand.support_href``) — an
    email or URL the site configured. When a ``problem`` turns the viewer away
    and a recourse is set, it is rendered as an actionable link instead of a
    sentence they can do nothing with. When it is "", the problem text stands
    alone: honest about the dead end rather than offering an empty link.
    """
    import html

    cli = html.escape(_cli_name())
    who = html.escape(handle)
    recourse_link = (
        f'<p><a href="{html.escape(recourse, quote=True)}">{html.escape(recourse_label)}</a></p>'
        if problem and recourse
        else ""
    )
    offered = list(sites or [])
    if len(offered) > 1:
        options = "".join(f'<option value="{html.escape(x, quote=True)}">{html.escape(x)}</option>'
                          for x in offered)
        site_choice = f'<p><label>For site <select id=site>{options}</select></label></p>'
    elif offered:
        site_choice = f"<p class=muted>For site <b>{html.escape(offered[0])}</b></p>"
    else:
        site_choice = ""
    body = (
        f"<p class=muted>Signed in as <b>{who}</b></p><p>{html.escape(problem)}</p>{recourse_link}"
        if problem
        else f"""<p class=muted>Signed in as <b>{who}</b></p>
<p>A key lets your tools (the command line, an agent) act as you. Yours will let them:</p>
<ul>{"".join(f"<li>{html.escape(_plain(sc))}</li>" for sc in scopes.split(", ") if sc)}</ul>
<p class=muted><small>Exact permissions: <code>{html.escape(scopes)}</code></small></p>
{site_choice}
<button id=mint>Create a key</button>
<div id=out hidden>
  <p><b>Store it now.</b> It is shown once. Run this, then paste the key when asked
  (what you paste stays hidden):</p>
  <pre>{cli} vault set {html.escape(store_name)}</pre>
  <p>Your key:</p><pre id=token></pre>
  <button id=copy>Copy key</button>
</div>
<p id=err class=err hidden></p>
<script>
document.getElementById("mint").onclick = async (e) => {{
  e.target.disabled = true;
  const pick = document.getElementById("site");
  const r = await fetch("/gate/keys", {{method: "POST", credentials: "same-origin",
    headers: {{"content-type": "application/json"}}, body: JSON.stringify({{site: pick ? pick.value : ""}})}});
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
