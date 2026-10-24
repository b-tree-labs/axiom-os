# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RotationProvider factory — issuer-side rotation for foreign credentials.

A ``RotationProvider`` knows how one *issuer class* mints a replacement
credential and how to verify the replacement works:

- ``GitLabPatProvider`` — ``POST /api/v4/personal_access_tokens/self/rotate``
  (GitLab >= 16.0, ``api`` or ``self_rotate`` scope). The old PAT dies on
  success — consequential, which is why the calling skill wraps the whole
  exchange in ``guarded_act``.
- ``GuidedRotationProvider`` — issuers without API rotation: a human
  opens the issuer token page and pastes the replacement. Fails closed
  when headless.

Secret values flow through this deterministic code into the store only —
never through an LLM, never over MCP, never into exception messages.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"

DEFAULT_ROTATION_EXPIRY_DAYS = 7  # owner directive: default rotation expiry +1 week


class ForeignRotationError(RuntimeError):
    """A rotation could not complete. NEVER carries a secret value."""


@dataclass(frozen=True, slots=True)
class ForeignRotationOutcome:
    """What a provider minted. ``handle`` is the issuer-side id (journal-safe)."""

    new_value: str
    expires_at: str | None = None
    handle: str | None = None


@runtime_checkable
class RotationProvider(Protocol):
    """How one issuer class rotates. Implementations set ``kind``."""

    kind: str
    interactive: bool

    def rotate(
        self, current: str, *, expires_at: str | None = None
    ) -> ForeignRotationOutcome: ...

    def probe(self, value: str) -> tuple[bool, str]: ...


def _default_expires_at() -> str:
    return (datetime.now(UTC) + timedelta(days=DEFAULT_ROTATION_EXPIRY_DAYS)).date().isoformat()


# --- GitLab PAT self-rotation ----------------------------------------------


class GitLabPatProvider:
    """Rotate a GitLab personal access token via the self-rotate API.

    ``http`` is an ``httpx.Client``-shaped object (``request(method, url,
    headers=..., params=...) -> Response``); tests inject one backed by
    ``httpx.MockTransport`` so no live issuer is ever called. The default
    client is built lazily on first use.
    """

    kind = "gitlab-pat"
    interactive = False

    def __init__(self, *, base_url: str, http: Any | None = None) -> None:
        self._base = (base_url or "").rstrip("/")
        self._http = http

    def _client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=30.0)
        return self._http

    def rotate(
        self, current: str, *, expires_at: str | None = None
    ) -> ForeignRotationOutcome:
        if not self._base:
            raise ForeignRotationError(
                "gitlab-pat rotation needs the issuer URL (metadata "
                "`issuer_url`, e.g. https://gitlab.example.org)"
            )
        expiry = expires_at or _default_expires_at()
        resp = self._client().request(
            "POST",
            f"{self._base}/api/v4/personal_access_tokens/self/rotate",
            headers={"PRIVATE-TOKEN": current},
            params={"expires_at": expiry},
        )
        if resp.status_code == 405:
            raise ForeignRotationError(
                "issuer refused self-rotation (HTTP 405) — GitLab >= 16.0 "
                "is required for POST /personal_access_tokens/self/rotate; "
                "fall back to `--provider guided`"
            )
        if resp.status_code != 200:
            # Never echo the request (it carries the token header).
            raise ForeignRotationError(
                f"issuer rotation failed (HTTP {resp.status_code}) — check "
                "the PAT is valid and holds the `api` (or `self_rotate`) "
                "scope; fall back to `--provider guided` if the issuer "
                "cannot self-rotate"
            )
        data = resp.json()
        token = data.get("token")
        if not token:
            raise ForeignRotationError(
                "issuer rotation response had no usable token field"
            )
        return ForeignRotationOutcome(
            new_value=token,
            expires_at=data.get("expires_at") or expiry,
            handle=str(data["id"]) if data.get("id") is not None else None,
        )

    def probe(self, value: str) -> tuple[bool, str]:
        resp = self._client().request(
            "GET",
            f"{self._base}/api/v4/user",
            headers={"PRIVATE-TOKEN": value},
        )
        if resp.status_code == 200:
            username = ""
            try:
                username = resp.json().get("username", "")
            except Exception:  # noqa: BLE001 — probe detail is best-effort
                pass
            return True, f"GET /api/v4/user ok{f' as {username}' if username else ''}"
        return False, f"GET /api/v4/user returned HTTP {resp.status_code}"


# --- Guided (interactive) fallback -----------------------------------------


class GuidedRotationProvider:
    """Human-in-the-loop rotation for issuers without a rotation API.

    ``user_prompt`` is the SkillContext's interactive callback; headless
    invocations fail closed with an actionable error. ``prober`` is an
    optional ``(value) -> (ok, detail)`` verification callable.
    """

    kind = "guided"
    interactive = True

    def __init__(
        self,
        *,
        issuer_url: str | None = None,
        user_prompt: Callable[[str], str] | None = None,
        prober: Callable[[str], tuple[bool, str]] | None = None,
    ) -> None:
        self._issuer_url = issuer_url
        self._prompt = user_prompt
        self._prober = prober

    def rotate(
        self, current: str, *, expires_at: str | None = None
    ) -> ForeignRotationOutcome:
        if self._prompt is None:
            raise ForeignRotationError(
                "guided rotation is interactive-only: a human must mint the "
                "replacement at the issuer console and paste it. Re-run "
                f"`{_brand_cli()} secrets rotate <name>` in a terminal."
            )
        where = self._issuer_url or "the issuer's token console"
        pasted = self._prompt(
            f"Mint a replacement credential at {where}, then paste it here "
            "(input is not echoed into any log): "
        )
        if not pasted:
            raise ForeignRotationError(
                "no replacement value was pasted; rotation aborted "
                "(the existing credential is unchanged)"
            )
        return ForeignRotationOutcome(new_value=pasted, expires_at=expires_at)

    def probe(self, value: str) -> tuple[bool, str]:
        if self._prober is not None:
            return self._prober(value)
        return True, "not probed (guided provider has no issuer probe wired)"



# --- Box OAuth refresh-token rotation ---------------------------------------

#: Box fixes the refresh-token lifetime at 60 days from last use. We cannot ask
#: for a different one, so the only honest thing to do is record what Box will
#: actually do.
BOX_REFRESH_LIFETIME_DAYS = 60
_BOX_TOKEN_URL = "https://api.box.com/oauth2/token"
_BOX_ME_URL = "https://api.box.com/2.0/users/me"


class BoxOAuthProvider:
    """Rotate a Box OAuth refresh token by refreshing it.

    Unlike every other provider here there is no separate rotate endpoint:
    Box's refresh tokens are **single-use**, so presenting one returns a
    replacement and invalidates what you presented. The refresh IS the
    rotation, which has two consequences worth stating.

    **Running this on a schedule is the mechanism, not hygiene.** The token
    expires 60 days after its LAST USE, so nothing ages it out except silence.
    This deployment lost one exactly that way: ingest stopped, nothing
    refreshed, and the credential was dead four weeks before anyone looked.
    KEEP calling this hourly means it cannot happen again.

    **The caller MUST persist ``new_value``.** A rotation whose result is
    dropped does not leave the old token working, it leaves nothing working,
    because Box has already invalidated it.
    """

    kind = "box-oauth"
    interactive = False

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_store: str | None = None,
        http: Any | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_store = token_store
        self._http = http

    def _client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=30.0)
        return self._http

    def rotate(
        self, current: str, *, expires_at: str | None = None
    ) -> ForeignRotationOutcome:
        if not (self._client_id and self._client_secret):
            raise ForeignRotationError(
                "box-oauth rotation needs client_id and client_secret "
                "(metadata `issuer_config`)"
            )
        resp = self._client().request(
            "POST",
            _BOX_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": current,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        if resp.status_code != 200:
            # Never echo the request or `current` — the body carries the token.
            detail = ""
            try:
                detail = str(resp.json().get("error", ""))
            except Exception:  # noqa: BLE001
                pass
            raise ForeignRotationError(
                f"Box refresh failed (HTTP {resp.status_code}"
                f"{f', {detail}' if detail else ''}). A Box refresh token dies "
                f"{BOX_REFRESH_LIFETIME_DAYS} days after its LAST USE, so "
                "disuse is the usual cause rather than age; re-run the login "
                "flow to mint a new one"
            )
        body = resp.json()
        new_rt = body.get("refresh_token")
        if not new_rt:
            # Falling back to `current` would look like success while leaving a
            # token Box has already invalidated.
            raise ForeignRotationError(
                "Box response carried no refresh_token; the presented one is "
                "already spent, so there is nothing safe to fall back to"
            )
        # `expires_at` is ignored on purpose: the caller does not get to choose.
        expiry = (
            datetime.now(UTC) + timedelta(days=BOX_REFRESH_LIFETIME_DAYS)
        ).date().isoformat()
        return ForeignRotationOutcome(new_value=new_rt, expires_at=expiry)

    def probe(self, value: str) -> tuple[bool | None, str]:
        """Check liveness WITHOUT spending the credential.

        The obvious probe — refresh and see if it works — consumes the token,
        so it is not a probe at all. This reads the cached *access* token,
        which is ordinary and re-presentable, and returns ``None`` when there
        is none. ``None`` renders blank in the CLI; ``False`` would say DEAD
        about a credential that is very likely fine.
        """
        cached = self._cached_access_token()
        if not cached:
            return None, (
                "no cached access token, and a Box refresh token cannot be "
                "probed without consuming it"
            )
        resp = self._client().request(
            "GET", _BOX_ME_URL, headers={"Authorization": f"Bearer {cached}"}
        )
        if resp.status_code == 200:
            login = ""
            try:
                login = resp.json().get("login", "")
            except Exception:  # noqa: BLE001 — probe detail is best-effort
                pass
            return True, f"GET /users/me ok{f' as {login}' if login else ''}"
        return False, f"GET /users/me returned HTTP {resp.status_code}"

    def _cached_access_token(self) -> str:
        if not self._token_store:
            return ""
        try:
            import json as _json
            from pathlib import Path as _Path

            return str(
                _json.loads(_Path(self._token_store).read_text()).get(
                    "access_token", ""
                )
            )
        except Exception:  # noqa: BLE001 — an unreadable store is "unknown"
            return ""


# --- factory ----------------------------------------------------------------


_KINDS: dict[str, type] = {
    BoxOAuthProvider.kind: BoxOAuthProvider,
    GitLabPatProvider.kind: GitLabPatProvider,
    GuidedRotationProvider.kind: GuidedRotationProvider,
}


def rotation_provider_kinds() -> list[str]:
    return sorted(_KINDS)


def build_rotation_provider(
    kind: str,
    *,
    issuer_url: str | None = None,
    http: Any | None = None,
    user_prompt: Callable[[str], str] | None = None,
    issuer_config: dict[str, Any] | None = None,
) -> RotationProvider:
    """Factory: ``kind`` → configured provider. Unknown kinds raise
    ``KeyError`` listing what IS known — never a silent substitution."""
    if kind == BoxOAuthProvider.kind:
        cfg = issuer_config or {}
        return BoxOAuthProvider(
            client_id=str(cfg.get("client_id", "")),
            client_secret=str(cfg.get("client_secret", "")),
            token_store=cfg.get("token_store"),
            http=http,
        )
    if kind == GitLabPatProvider.kind:
        return GitLabPatProvider(base_url=issuer_url or "", http=http)
    if kind == GuidedRotationProvider.kind:
        return GuidedRotationProvider(
            issuer_url=issuer_url, user_prompt=user_prompt,
        )
    raise KeyError(
        f"no RotationProvider for kind={kind!r}; known kinds: "
        f"{', '.join(rotation_provider_kinds())}"
    )


__all__ = [
    "BoxOAuthProvider",
    "DEFAULT_ROTATION_EXPIRY_DAYS",
    "ForeignRotationError",
    "ForeignRotationOutcome",
    "GitLabPatProvider",
    "GuidedRotationProvider",
    "RotationProvider",
    "build_rotation_provider",
    "rotation_provider_kinds",
]
