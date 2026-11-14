# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Sign in to a node from the command line, and keep the key without anyone seeing it.

One call: the browser opens the node's ``/gate/cli/authorize`` page; the person signs
in the way their site offers (a university account, or an invitation code); the node
sends a one-time code back to a :class:`LoopbackListener` on this computer; this
module trades it, with the PKCE verifier only this process holds, for a key; the key
goes straight into this machine's vault under the name the node's Keys page uses
(``<first label of the host>-key``). The key is never printed, never written to a
file outside the vault and never passed on a command line.

Everything that touches the outside world is injected (``open_browser``,
``post_json``, ``store``) so the whole round trip runs in a test against an
in-process node and a real listener.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse

from .loopback import LoopbackListener
from .pkce import generate_pkce

AUTHORIZE_PATH = "/gate/cli/authorize"
TOKEN_PATH = "/gate/cli/token"
REDEEM_PATH = "/gate/redeem"

#: Seconds to wait for the person to finish signing in. Long enough for a
#: multi-factor prompt on a phone; the listener stops the moment it is answered.
DEFAULT_TIMEOUT_S = 300.0

PostJson = Callable[[str, dict, float], tuple[int, dict]]


@dataclass(frozen=True)
class SignInResult:
    ok: bool
    detail: str = ""
    remedy: str = ""
    #: What the node said about the key it minted. Never the key itself.
    principal: str = ""
    key_id: str = ""
    site: str = ""
    scopes: tuple[str, ...] = field(default_factory=tuple)
    #: The vault name the key was stored under, and the reference a binding records.
    stored_as: str = ""
    token_ref: str = ""
    replaced: bool = False


def key_name_for(face_url: str) -> str:
    """The vault name the node's Keys page tells people to use: ``<first label>-key``."""
    host = urlparse(face_url).hostname or ""
    label = host.split(".")[0] if host else ""
    return f"{label}-key" if label else ""


def token_ref_for(name: str) -> str:
    """The reference that resolves through the same store ``vault set <name>`` writes."""
    return f"foreign://{name}"


def _post_json(url: str, body: dict, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(  # noqa: S310 - scheme checked by the caller
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        data = {}
    return status, data if isinstance(data, dict) else {}


def _open_browser(url: str) -> bool:
    import webbrowser

    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 - no browser is a reason to print the address
        return False


def default_store():
    """This machine's vault, as ``vault set`` and every reader of a key open it."""
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.infra.paths import get_user_state_dir

    return ForeignCredentialStore(get_user_state_dir())


def authorize_url(face_url: str, *, redirect_uri: str, state: str, challenge: str, site: str = "") -> str:
    params = {
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if site:
        params["site"] = site
    return face_url.rstrip("/") + AUTHORIZE_PATH + "?" + urlencode(params)


def sign_in(
    face_url: str,
    *,
    site: str = "",
    open_browser: Callable[[str], bool] = _open_browser,
    post_json: PostJson = _post_json,
    store: Any = None,
    say: Callable[[str], None] = print,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> SignInResult:
    """Run the whole browser sign-in against the node at *face_url*. Never raises."""
    parsed = urlparse(face_url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return SignInResult(
            ok=False,
            detail=f"{face_url!r} is not a web address",
            remedy="give the node's base address, for example https://node.example.org",
        )
    state = secrets.token_urlsafe(24)
    verifier, challenge = generate_pkce()

    with LoopbackListener(state) as listener:
        url = authorize_url(
            face_url, redirect_uri=listener.redirect_uri, state=state, challenge=challenge, site=site
        )
        if open_browser(url):
            say("Opened your browser to sign in. Waiting for you to finish there ...")
        else:
            say("Open this address in a browser on this computer to sign in:")
            say(f"  {url}")
        answer = listener.wait(timeout_s)
        redirect_uri = listener.redirect_uri

    if answer is None:
        return SignInResult(
            ok=False,
            detail=f"no sign-in arrived within {int(timeout_s // 60)} minutes",
            remedy="run this again and finish signing in in the browser that opens",
        )
    if not answer.code:
        said = answer.error_description or answer.error or "the sign-in was not completed"
        return SignInResult(ok=False, detail=f"the node said: {said}", remedy=_remedy_for(said))

    try:
        status, data = post_json(
            face_url.rstrip("/") + TOKEN_PATH,
            {"code": answer.code, "code_verifier": verifier, "redirect_uri": redirect_uri},
            30.0,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return SignInResult(
            ok=False,
            detail=f"could not reach {face_url} to collect the key: {getattr(exc, 'reason', exc)}",
            remedy="check the network (and the VPN, off campus), then run this again",
        )
    if status != 200 or not isinstance(data.get("token"), str) or not data.get("token"):
        said = str(data.get("detail") or f"HTTP {status}")
        return SignInResult(ok=False, detail=f"the node did not issue a key: {said}",
                            remedy=_remedy_for(said))

    return _keep(face_url, data, store, how="signed in")


def redeem_invitation(
    face_url: str,
    code: str,
    *,
    post_json: PostJson = _post_json,
    store: Any = None,
) -> SignInResult:
    """Present an invitation code without a browser, and keep the key the same way.

    For a terminal with no browser (a remote shell, a test runner). The code comes
    from a hidden prompt or standard input, never the command line.
    """
    parsed = urlparse(face_url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return SignInResult(ok=False, detail=f"{face_url!r} is not a web address",
                            remedy="give the node's base address, for example https://node.example.org")
    code = (code or "").strip()
    if not code:
        return SignInResult(ok=False, detail="no invitation code",
                            remedy="paste the code your site administrator sent when asked")
    try:
        status, data = post_json(face_url.rstrip("/") + REDEEM_PATH, {"code": code}, 30.0)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return SignInResult(
            ok=False,
            detail=f"could not reach {face_url}: {getattr(exc, 'reason', exc)}",
            remedy="check the network (and the VPN, off campus). The invitation was not spent.",
        )
    if status != 200 or not isinstance(data.get("token"), str) or not data.get("token"):
        said = str(data.get("detail") or f"HTTP {status}")
        return SignInResult(ok=False, detail=f"the node did not issue a key: {said}",
                            remedy=_remedy_for(said))
    return _keep(face_url, data, store, how="redeemed an invitation")


def _keep(face_url: str, data: dict, store: Any, *, how: str) -> SignInResult:
    """Put the key from a node's reply into the vault, then forget it. Never prints it."""
    name = key_name_for(face_url)
    key_id = str(data.get("key_id") or "")
    the_store = store if store is not None else default_store()
    try:
        replaced = the_store.exists(name)
        the_store.set(
            name,
            data["token"].encode("utf-8"),
            provider="axiom-api-key",
            notes=f"api scope: {how} at {urlparse(face_url).hostname}"
            + (f" (key {key_id})" if key_id else ""),
            issuer_url=face_url.rstrip("/"),
        )
    except Exception as exc:  # noqa: BLE001 - whatever the vault says, the key is not stored
        return SignInResult(
            ok=False,
            key_id=key_id,
            detail=f"the node issued a key but this computer's vault would not store it "
            f"({type(exc).__name__})",
            remedy="run the vault step again (setup repairs it), then sign in again. "
            + (f"Ask your site administrator to revoke key {key_id}. " if key_id else "")
            + "The key was not written anywhere.",
        )
    finally:
        data.pop("token", None)

    return SignInResult(
        ok=True,
        detail="key stored in the vault",
        principal=str(data.get("principal") or ""),
        key_id=key_id,
        site=str(data.get("site") or ""),
        scopes=tuple(str(s) for s in (data.get("scopes") or ())),
        stored_as=name,
        token_ref=token_ref_for(name),
        replaced=replaced,
    )


def _remedy_for(said: str) -> str:
    low = said.lower()
    if "no role" in low or "not define" in low or "grant" in low:
        return "ask your site administrator to grant you a role, then run this again"
    if "invitation" in low:
        return "ask your site administrator for a new invitation, then run this again"
    if "cancel" in low:
        return "run this again when you are ready"
    return "run this again; if it repeats, tell your site administrator what it said"


__all__ = [
    "AUTHORIZE_PATH",
    "DEFAULT_TIMEOUT_S",
    "TOKEN_PATH",
    "SignInResult",
    "authorize_url",
    "default_store",
    "key_name_for",
    "redeem_invitation",
    "sign_in",
    "token_ref_for",
]
