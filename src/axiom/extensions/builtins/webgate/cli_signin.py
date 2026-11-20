# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The command line's browser sign-in: the rules and the one-time code store.

A person types one command. Their browser opens the node's sign-in page; they sign
in the way they always do (a university account, or an invitation code); the node
hands a one-time code to a listener on their own computer, and the command line
trades it for a key that goes straight into their vault. They never see the key,
and nothing is copied. It is the loopback pattern every modern command line uses
(RFC 8252, the shape of ``gh auth login`` and ``gcloud auth login``).

This module holds what the routes must get right and nothing about HTML:

- **Where a code may be sent.** Only to ``http://127.0.0.1:<port>/callback`` or
  ``http://[::1]:<port>/callback``. Not ``localhost``, which a hosts file or a DNS
  answer can point somewhere else, and never anything with a path, a query, a
  fragment or a user name in it. A request naming anywhere else is refused in place
  and never redirected, so the route cannot become an open redirect.
- **What the code is bound to.** The PKCE challenge (S256 only), the exact redirect
  address, and a short expiry. The code is single-use: it is removed the moment it
  is presented, valid or not.
- **What is kept.** Only a hash of each code. A signed-in grant keeps the decision
  (who, which roles, which site), which is not a secret. An invitation grant keeps
  the invitation code *sealed under the one-time code*, which this node never
  stores, so the file alone opens nothing. The key itself is minted only when the
  code is exchanged, so an abandoned sign-in leaves no key behind and spends no
  invitation.

The store is a small JSON file beside the keys file, written under a lock, because a
node runs more than one worker and, during a blue/green switch, more than one slot.
An in-memory store would let the browser leg land on one and the exchange on the
other.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

#: Seconds between the browser leg finishing and the command line collecting the
#: key. The command line exchanges the code the moment it arrives; this only has to
#: cover a slow network.
CODE_TTL_S = 120

#: The one path a loopback listener answers on. Fixed so a request cannot steer the
#: code to some other local service that happens to listen on the same port.
CALLBACK_PATH = "/callback"

_STATE_RE = re.compile(r"[A-Za-z0-9_\-]{16,128}")
_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_\-]{43}")
_VERIFIER_RE = re.compile(r"[A-Za-z0-9._~\-]{43,128}")

#: Hosts a code may be sent to: the machine itself, by address.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


class SignInRequestError(ValueError):
    """The sign-in request is malformed or asks for something not allowed.

    The message is safe to show: it names the field, never a value.
    """


@dataclass(frozen=True)
class SignInRequest:
    """A validated request from a command line waiting on this computer."""

    redirect_uri: str
    state: str
    code_challenge: str
    site: str = ""


def loopback_problem(uri: str) -> str | None:
    """Why *uri* may not receive a code, or ``None`` when it may."""
    if not uri or len(uri) > 200:
        return "the return address is missing or too long"
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        return "the return address is not a valid address"
    if parts.scheme != "http":
        return "the return address must be http on this computer"
    if "@" in parts.netloc:
        return "the return address may not carry a user name"
    if parts.hostname not in _LOOPBACK_HOSTS:
        return "the return address must be 127.0.0.1 or [::1] (this computer)"
    if port is None or not 1024 <= port <= 65535:
        return "the return address must name a port between 1024 and 65535"
    if parts.path != CALLBACK_PATH or parts.query or parts.fragment:
        return f"the return address must end in {CALLBACK_PATH} with nothing after it"
    return None


def parse_request(params: dict[str, Any]) -> SignInRequest:
    """Validate the query (or form) a command line sent. Raises :class:`SignInRequestError`."""
    redirect_uri = str(params.get("redirect_uri") or "")
    problem = loopback_problem(redirect_uri)
    if problem:
        raise SignInRequestError(problem)
    state = str(params.get("state") or "")
    if not _STATE_RE.fullmatch(state):
        raise SignInRequestError("the request's state is missing or malformed")
    challenge = str(params.get("code_challenge") or "")
    if not _CHALLENGE_RE.fullmatch(challenge):
        raise SignInRequestError("the request's code challenge is missing or malformed")
    method = str(params.get("code_challenge_method") or "")
    if method != "S256":
        raise SignInRequestError("only the S256 code challenge method is accepted")
    site = str(params.get("site") or "").strip()
    return SignInRequest(redirect_uri=redirect_uri, state=state, code_challenge=challenge, site=site)


def pkce_matches(verifier: str, challenge: str) -> bool:
    """RFC 7636 S256: does *verifier* hash to *challenge*? Constant-time."""
    if not isinstance(verifier, str) or not _VERIFIER_RE.fullmatch(verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, challenge)


def _lookup(code: str) -> str:
    return hashlib.sha256(b"axiom-cli-signin:id:" + code.encode("utf-8")).hexdigest()


def _sealer(code: str):
    from cryptography.fernet import Fernet

    key = hashlib.sha256(b"axiom-cli-signin:seal:" + code.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


class CodeStore:
    """One-time codes, by hash, in a locked JSON file. Expired entries are dropped on write."""

    def __init__(self, path: Path, *, now: Callable[[], float] = time.time, ttl_s: int = CODE_TTL_S):
        self.path = Path(path)
        self._now = now
        self._ttl = ttl_s

    def _locked(self):
        from axiom.infra.state import LockedJsonFile

        return LockedJsonFile(self.path, exclusive=True)

    def _live(self, data: Any) -> dict[str, dict]:
        rows = data.get("codes") if isinstance(data, dict) else None
        now = self._now()
        return {
            k: v
            for k, v in (rows or {}).items()
            if isinstance(v, dict) and float(v.get("expires_at") or 0) > now
        }

    def _issue(self, request: SignInRequest, record_for: Callable[[str], dict]) -> str:
        """Mint a code, store its entry under the code's hash, return the code."""
        code = secrets.token_urlsafe(32)
        entry = {
            "redirect_uri": request.redirect_uri,
            "code_challenge": request.code_challenge,
            "expires_at": self._now() + self._ttl,
            **record_for(code),
        }
        with self._locked() as f:
            rows = self._live(f.read())
            rows[_lookup(code)] = entry
            f.write({"codes": rows})
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return code

    def issue_for_session(self, request: SignInRequest, grant: dict) -> str:
        """A code for a signed-in person; *grant* is the decision, not a secret."""
        return self._issue(request, lambda _code: {"kind": "session", "grant": dict(grant)})

    def issue_for_invitation(self, request: SignInRequest, invitation_code: str) -> str:
        """A code for an invitation, which stays sealed until the code comes back."""

        def sealed(code: str) -> dict:
            box = _sealer(code).encrypt(invitation_code.encode("utf-8")).decode("ascii")
            return {"kind": "invitation", "sealed": box}

        return self._issue(request, sealed)

    def consume(self, code: str) -> dict | None:
        """Remove and return the entry for *code*, or ``None`` if unknown or expired.

        Removed first, whatever happens next: a code is spent the moment it is
        presented, so a replay, or a second try after a failed check, finds nothing.
        """
        if not isinstance(code, str) or not code or len(code) > 200:
            return None
        key = _lookup(code)
        with self._locked() as f:
            data = f.read()
            raw = dict((data or {}).get("codes") or {}) if isinstance(data, dict) else {}
            entry = raw.pop(key, None)
            f.write({"codes": self._live({"codes": raw})})
        if not isinstance(entry, dict) or float(entry.get("expires_at") or 0) <= self._now():
            return None
        return entry


def unseal_invitation(entry: dict, code: str) -> str:
    """The invitation code an invitation entry holds, opened with its one-time code."""
    return _sealer(code).decrypt(str(entry.get("sealed") or "").encode("ascii")).decode("utf-8")


def store_path_for(keys_path: Path | None) -> Path | None:
    """Where the code store lives: beside the keys file, or nowhere if there is none."""
    return keys_path.with_name("cli-sign-ins.json") if keys_path is not None else None


__all__ = [
    "CALLBACK_PATH",
    "CODE_TTL_S",
    "CodeStore",
    "SignInRequest",
    "SignInRequestError",
    "loopback_problem",
    "parse_request",
    "pkce_matches",
    "store_path_for",
    "unseal_invitation",
]
