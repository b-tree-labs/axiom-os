# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/gate/import/commit`` — a person commits an import from the web, at the node's gate (#1804).

The web app shows an import's dry run (``POST /api/v1/attest/{log}/imports/dry-run``), which keeps
it for a person who holds the importer role and names this page. The commit happens here, the way
the command line's signing hand-off does (#1936): the gate's own page, the person's own sign-in,
one button. There is no HTTP route that commits an import, and a key or an agent cannot reach
this one: it reads only the gate session (ADR-262).

- ``GET /gate/import/commit?prepared_id=…`` — signed out, the gate's sign-in and back. Signed in,
  the dry run as the server computed it (counts, warnings, the digest) and one button.
- ``POST /gate/import/commit`` — that button. Commits the kept dry run as the session's person,
  only if they hold the importer role for the log now; the kept dry run is then spent. The
  browser returns to the page that asked, with ``import_batch`` added.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

log = logging.getLogger(__name__)

COMMIT_PATH = "/gate/import/commit"
LOGIN_PATH = "/gate/login"
_NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}


def _state_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir())


def register_import_commit_routes(router: APIRouter, *, issuer_of: Callable[[Request], str]) -> None:
    from axiom.webauth import session_from_cookies

    def person(request: Request) -> tuple[str | None, dict[str, Any] | None]:
        from axiom.extensions.builtins.attest.naming import signer_handle

        claims = session_from_cookies(request.cookies, issuer=issuer_of(request))
        if not claims:
            return None, None
        try:
            return signer_handle(claims), claims
        except Exception:  # noqa: BLE001 - a sign-in that cannot be named commits nothing
            return None, claims

    def kept(prepared_id: str) -> dict[str, Any] | None:
        from axiom.extensions.builtins.attest import imports

        return imports.prepared(prepared_id, state_dir=_state_dir())

    @router.get(COMMIT_PATH)
    async def commit_page(request: Request) -> Response:
        from axiom.extensions.builtins.attest import imports

        prepared_id = str(request.query_params.get("prepared_id") or "")
        handle, claims = person(request)
        if claims is None:
            here = quote(f"{COMMIT_PATH}?prepared_id={quote(prepared_id)}", safe="")
            return RedirectResponse(f"{LOGIN_PATH}?next={here}", status_code=303, headers=_NO_STORE)
        p = kept(prepared_id)
        if p is None:
            return _page("Nothing to import", "<p>This import is unknown, already done, or its dry "
                         "run is too old. Run the dry run again.</p>", 404)  # fmt: skip
        if handle is None:
            return _page("Not imported", "<p>This sign-in cannot be named.</p>", 403)
        try:
            dry = imports.dry_run(p["site_id"], p["logbook"], p["content"].encode("utf-8"),
                                  source=p["source"])  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - shown, never a crash
            return _page("Not imported", f"<p>{html.escape(str(exc))}</p>", 422)
        return _confirm(p, dry, handle)

    @router.post(COMMIT_PATH)
    async def commit(request: Request) -> Response:
        from axiom.extensions.builtins.attest import imports
        from axiom.extensions.builtins.attest.service import AttestRefused

        form = dict(await request.form())
        prepared_id = str(form.get("prepared_id") or "")
        handle, claims = person(request)
        if claims is None:
            here = quote(f"{COMMIT_PATH}?prepared_id={quote(prepared_id)}", safe="")
            return RedirectResponse(f"{LOGIN_PATH}?next={here}", status_code=303, headers=_NO_STORE)
        if handle is None:
            return _page("Not imported", "<p>This sign-in cannot be named.</p>", 403)
        p = kept(prepared_id)
        if p is None:
            return _page("Nothing to import", "<p>This import is unknown, already done, or its dry "
                         "run is too old. Run the dry run again.</p>", 404)  # fmt: skip
        site = claims.get("site")
        if site and site != p["site_id"]:
            return _page("Not imported", f"<p>This sign-in is for {html.escape(str(site))}, not "
                         f"{html.escape(p['site_id'])}.</p>", 403)  # fmt: skip
        try:
            done = imports.commit(p["site_id"], p["logbook"], p["content"].encode("utf-8"),
                                  digest=p["digest"], by=handle, source=p["source"], via="web",
                                  state_dir=_state_dir())  # fmt: skip
        except AttestRefused as exc:
            status = 403 if "importer role" in str(exc) else 422
            return _page("Not imported", f"<p>{html.escape(str(exc))}</p><p>Nothing was written.</p>",
                         status)  # fmt: skip
        imports.spend(prepared_id, state_dir=_state_dir())
        back = p.get("return_to")
        if back:
            joiner = "&" if "?" in back else "?"
            batch = done["batch"] or ""
            return RedirectResponse(f"{back}{joiner}import_batch={quote(batch)}&imported={done['written']}",
                                    status_code=303, headers=_NO_STORE)  # fmt: skip
        return _page("Imported", f"<p>{done['written']} entries were placed on the chain as "
                     f"imported, not signed (batch {html.escape(done['batch'] or 'none')}).</p>", 200)  # fmt: skip


def _confirm(p: dict[str, Any], dry: dict[str, Any], handle: str) -> HTMLResponse:
    c = dry["counts"]
    warnings = "".join(f"<li>Line {w['line']} ({html.escape(str(w['source_ref']))}): "
                       f"{html.escape(w['warning'])}</li>" for w in dry["warnings"])  # fmt: skip
    if c["errors"] or not c["new"]:
        why = (f"The dry run found {c['errors']} error(s); fix the input and run it again."
               if c["errors"] else "Every entry is already on the chain.")  # fmt: skip
        return _page("Nothing to import", f"<p>{html.escape(why)}</p>", 409 if c["errors"] else 200)
    inner = (
        f"<p>Import into <b>{html.escape(p['logbook'])}</b> at {html.escape(p['site_id'])} as "
        f"<b>{html.escape(handle)}</b>. Each entry is placed on the chain as imported, not signed; "
        "nobody signs it here.</p>"
        f"<ul><li>{c['new']} new</li><li>{c['skipped']} already on the chain</li>"
        f"<li>{c['warnings']} warning(s)</li></ul>"
        + (f"<ul>{warnings}</ul>" if warnings else "")
        + f"<p>Source's own chain: {html.escape(str(dry['source_verified']))}</p>"
        f"<p>Digest <code>{html.escape(dry['digest'])}</code></p>"
        f'<form method="post" action="{COMMIT_PATH}">'
        f'<input type="hidden" name="prepared_id" value="{html.escape(p["prepared_id"], quote=True)}">'
        f'<button type="submit">Import {c["new"]} entries</button></form>'
        "<p>Close this page to leave it unimported. Nothing is written until you press the button.</p>"
    )
    return _page("Import history", inner, 200)


def _page(title: str, inner: str, status: int) -> HTMLResponse:
    body = (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<meta name='viewport' content='width=device-width'></head><body><main>"
            f"<h1>{html.escape(title)}</h1>{inner}</main></body></html>")  # fmt: skip
    return HTMLResponse(body, status_code=status,
                        headers={**_NO_STORE, "x-frame-options": "DENY",
                                 "content-security-policy": "frame-ancestors 'none'"})  # fmt: skip


__all__ = ["COMMIT_PATH", "register_import_commit_routes"]
