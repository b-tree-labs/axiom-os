# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The command line's half of signing at the node's gate (#1669, #1870).

A prompt proves neither an ``sso`` sign-in nor a fresh one, so an entry that needs either is
not signed on the prompt's word. Instead the command line opens (or prints) a one-time link to
the node's own gate (``/gate/cli/sign``), where the person signs in with the sign-in they
already have and presses one button. The gate mints the same signing grant the web confirm
card gets, bound to this presentation and this person and carrying the sign-in time, and sends
a one-time code back to a listener on 127.0.0.1. This module trades the code, with the PKCE
verifier only this process holds, for the grant.

Fails closed: no grant, no signature. An unreachable gate or a link nobody opens before the
timeout says so, and the draft stays in the person's tray. Nothing here prints, logs or passes
on a command line a code or a grant.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

SIGN_PATH = "/gate/cli/sign"
TOKEN_PATH = "/gate/cli/sign/token"
#: How long the command line waits for the person to finish in the browser.
DEFAULT_TIMEOUT_S = 300.0

PostJson = Callable[[str, dict, float], tuple[int, dict]]


@dataclass(frozen=True)
class Handoff:
    """Where the gate is and how to reach it; every part is a seam for tests."""

    face_url: str
    open_browser: Callable[[str], bool]
    post_json: PostJson
    say: Callable[[str], None]
    timeout_s: float = DEFAULT_TIMEOUT_S


def _post_json(url: str, body: dict, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"content-type": "application/json"})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - the node's own face
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def _open_browser(url: str) -> bool:
    try:
        import webbrowser

        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 - no browser: print the link instead
        return False


def _face_url() -> str:
    from axiom.extensions.builtins.notifications.channel_config import public_base_url

    return public_base_url()


_override: Handoff | None = None


def configure(handoff: Handoff | None) -> None:
    """Seam: where the gate is (tests point it at an in-process node)."""
    global _override
    _override = handoff


def current(say: Callable[[str], None] = print) -> Handoff | None:
    """The hand-off to use, or None when this node names no face to reach its gate at."""
    if _override is not None:
        return _override
    url = _face_url()
    if not url:
        return None
    return Handoff(face_url=url, open_browser=_open_browser, post_json=_post_json, say=say)


def obtain_grant(presentation_id: str, handoff: Handoff) -> tuple[str | None, str]:
    """``(grant, "")``, or ``(None, why)``. Never raises."""
    from axiom.extensions.builtins.auth.loopback import LoopbackListener
    from axiom.extensions.builtins.auth.pkce import generate_pkce

    parsed = urlparse(handoff.face_url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None, f"{handoff.face_url!r} is not the node's web address"
    state = secrets.token_urlsafe(24)
    verifier, challenge = generate_pkce()
    with LoopbackListener(state) as listener:
        link = handoff.face_url.rstrip("/") + SIGN_PATH + "?" + urlencode(
            {"redirect_uri": listener.redirect_uri, "state": state, "code_challenge": challenge,
             "code_challenge_method": "S256", "presentation_id": presentation_id}
        )  # fmt: skip
        if handoff.open_browser(link):
            handoff.say("Opened your browser at the node's gate to sign. Waiting for you there ...")
        else:
            handoff.say("Open this link in a browser on this computer to sign:")
            handoff.say(f"  {link}")
        answer = listener.wait(handoff.timeout_s)
        redirect_uri = listener.redirect_uri
    if answer is None:
        return None, f"nothing came back from the gate within {int(handoff.timeout_s)} seconds"
    if not answer.code:
        return None, f"the gate said: {answer.error_description or answer.error or 'not signed'}"
    try:
        status, data = handoff.post_json(
            handoff.face_url.rstrip("/") + TOKEN_PATH,
            {"code": answer.code, "code_verifier": verifier, "redirect_uri": redirect_uri},
            30.0,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"could not reach the gate at {handoff.face_url}: {getattr(exc, 'reason', exc)}"
    grant = data.get("grant") if isinstance(data, dict) else None
    if status != 200 or not isinstance(grant, str) or not grant:
        said = (data or {}).get("detail") if isinstance(data, dict) else None
        return None, f"the gate did not hand over a signing proof: {said or f'HTTP {status}'}"
    return grant, ""


__all__ = ["DEFAULT_TIMEOUT_S", "Handoff", "configure", "current", "obtain_grant"]
