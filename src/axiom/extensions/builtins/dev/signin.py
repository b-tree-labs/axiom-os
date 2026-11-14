# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Find a sign-in client in the vault a local node can use.

A deployed node is told its OIDC client in its environment. A local one should
not need to be: if the person running it already holds an OIDC client
credential in their vault, recorded with the issuer it belongs to and its
client id, the node can offer that sign-in with nothing to configure.

Only the credential's *name* travels to the node. The gate reads the value out
of the vault itself (``AXIOM_GATE_OIDC_CLIENT_SECRET_VAULT``), so the secret
never sits in the node's environment, where the process table would show it.

Two kinds of client (#1717):

* **confidential**: the entry holds the client's secret. The app owner's flow.
* **public**: ``client_type = "public"``, or an entry with no value recorded.
  Authorization code + PKCE, no secret anywhere: the node is told the issuer
  and client id and nothing else, and never reads the entry's value. A
  contributor's laptop gets one from the site node it is bound to
  (:func:`from_bound_site`), so nobody copies a secret to a laptop.

A confidential client wins when the vault holds both.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

#: The environment block `oidc.OidcSignIn.from_env` reads.
PREFIX = "AXIOM_GATE_OIDC_"

#: Issuers whose tenant is part of the issuer URL. The gate has a preset for
#: this shape, which validates the tenant; a plain discovery URL would not.
_TENANT_ISSUERS = {"login.microsoftonline.com": "entra"}


@dataclass(frozen=True)
class SignInClient:
    credential: str
    issuer: str
    client_id: str
    provider: str
    tenant: str = ""
    public: bool = False

    def environment(self, *, label: str = "") -> dict[str, str]:
        env = {
            PREFIX + "PROVIDER": self.provider,
            PREFIX + "CLIENT_ID": self.client_id,
        }
        if self.public:
            # No secret exists, so none is named. The account is keyed on the
            # IdP subject, as the site keys it, so local work is attributed to
            # the person's site principal. The person running this node owns
            # it, whatever app roles the site's registration hands out.
            env[PREFIX + "CLIENT_TYPE"] = "public"
            env[PREFIX + "IDP_ROLES"] = "0"
            env["AXIOM_GATE_JIT_ROLE"] = "owner"
        else:
            env[PREFIX + "CLIENT_SECRET_VAULT"] = self.credential
        if self.tenant:
            env[PREFIX + "TENANT"] = self.tenant
        if label:
            env[PREFIX + "LABEL"] = label
        return env

    @property
    def discovered(self) -> bool:
        """The gate reads this issuer's discovery document; a preset needs none."""
        return self.provider.startswith("https://")

    @property
    def issuer_host(self) -> str:
        return urlsplit(self.issuer).hostname or self.issuer

    @property
    def label(self) -> str:
        if self.provider == "entra":
            return "Sign in with Microsoft"
        return f"Sign in with {urlsplit(self.issuer).hostname}"


def answers(client: SignInClient, *, fetch: Callable[[str], Any] | None = None) -> bool:
    """Whether the gate will be able to read ``client``'s issuer just now.

    A gate whose issuer does not answer stands and refuses SSO (#1842), so the
    `Running:` block has to say that instead of promising a sign-in. A preset
    provider is never read at start, so it always answers here. Never raises."""
    if not client.discovered:
        return True
    url = client.provider.rstrip("/") + "/.well-known/openid-configuration"
    try:
        (fetch or _fetch)(url)
    except Exception:  # noqa: BLE001 - any failure is "does not answer"
        return False
    return True


def _fetch(url: str) -> None:
    from urllib.request import Request, urlopen

    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=3) as resp:  # noqa: S310 - https issuer from the vault
        resp.read(1)


def is_public(meta: dict[str, Any]) -> bool:
    """A public client: declared so, or an entry with no value recorded."""
    return str(meta.get("client_type") or "").strip().lower() == "public" or not meta.get(
        "fingerprint"
    )


def _client_from(meta: dict[str, Any]) -> SignInClient | None:
    issuer = str(meta.get("issuer_url") or "").strip()
    client_id = str(meta.get("client_id") or "").strip()
    name = str(meta.get("name") or "").strip()
    if not (issuer and client_id and name):
        return None
    parts = urlsplit(issuer)
    if parts.scheme != "https" or not parts.hostname:
        return None
    kind = _TENANT_ISSUERS.get(parts.hostname)
    if kind:
        tenant = next((p for p in parts.path.split("/") if p), "")
        if not tenant:
            return None
        return SignInClient(name, issuer, client_id, kind, tenant, public=is_public(meta))
    return SignInClient(name, issuer, client_id, issuer, public=is_public(meta))


def detect(entries: Iterable[dict[str, Any]]) -> tuple[SignInClient | None, list[str]]:
    """The one sign-in client the vault holds, and anything worth saying.

    More than one is not guessed between: the caller names the one it wants.
    """
    every = [c for c in (_client_from(m) for m in entries) if c is not None]
    # A confidential client wins: the owner's flow is unchanged by a public
    # entry sitting beside it.
    found = [c for c in every if not c.public] or every
    if not found:
        return None, []
    if len(found) > 1:
        names = ", ".join(sorted(c.credential for c in found))
        return None, [
            f"the vault holds more than one sign-in client ({names}); "
            "name one with --sign-in <credential>"
        ]
    return found[0], []


def choose(entries: Iterable[dict[str, Any]], name: str) -> SignInClient | None:
    for meta in entries:
        if meta.get("name") == name:
            return _client_from(meta)
    return None


# -- the bound site's public client -----------------------------------------

#: Where a bound site node publishes its public sign-in client.
SITE_SIGN_IN_PATH = "/api/v1/site/sign-in"
NOT_ENABLED = (
    "this site has not enabled SSO for local development; using password sign-in"
)


@dataclass(frozen=True)
class SiteOffer:
    """What the bound site node published: four fields, all of them public."""

    site: str
    issuer_url: str
    client_id: str


def _http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, Any]]:
    from axiom.infra import serving_mode

    return serving_mode._http_get(url, headers, timeout)


def from_bound_site(
    *, mode: Any = None, http_get: Callable[..., tuple[int, dict]] | None = None,
    key: str | None = None, timeout: float = 5.0,
) -> tuple[SiteOffer | None, list[str]]:
    """The public client the site this install is bound to publishes, and what to say.

    ``(None, [])`` when the install is not bound: nothing to ask. Only the bound
    node is asked, with the binding's own key. Never raises.
    """
    try:
        from axiom.infra import serving_mode

        m = mode if mode is not None else serving_mode.resolve_mode()
        if not getattr(m, "bound", False) or not getattr(m, "face_url", ""):
            return None, []
        face = str(m.face_url).rstrip("/")
        if key is None:
            from axiom.extensions.builtins.data_platform import read_target

            key = read_target.bound_key_for(m.face_url)
        headers = {"accept": "application/json"}
        if key:
            headers["authorization"] = f"Bearer {key}"
        status, body = (http_get or _http_get)(face + SITE_SIGN_IN_PATH, headers, timeout)
    except Exception as exc:  # noqa: BLE001 - said, never raised
        return None, [
            f"could not ask the bound site for its sign-in ({type(exc).__name__}); "
            "using password sign-in"
        ]
    site = str(getattr(m, "site", "") or "")
    if status == 404:
        return None, [NOT_ENABLED]
    if status in (401, 403):
        return None, [
            f"the bound site refused the sign-in lookup (HTTP {status}): the binding's key "
            "needs read access; using password sign-in"
        ]
    if status != 200:
        return None, [f"the bound site answered HTTP {status} to the sign-in lookup; using password sign-in"]
    issuer = str(body.get("issuer_url") or "").strip()
    client_id = str(body.get("client_id") or "").strip()
    parts = urlsplit(issuer)
    if (
        str(body.get("client_type") or "") != "public"
        or parts.scheme != "https"
        or not parts.hostname
        or not client_id
    ):
        return None, ["the bound site's sign-in answer was not a public client; using password sign-in"]
    return SiteOffer(site=site, issuer_url=issuer, client_id=client_id), []


def bound_site_name() -> str:
    """The site this install is bound to, or ``""``. Never raises."""
    try:
        from axiom.infra import serving_mode

        m = serving_mode.resolve_mode()
        return str(m.site or "") if m.bound else ""
    except Exception:  # noqa: BLE001
        return ""


def entry_name(site: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", site).strip("-.") or "site"
    return f"{slug}-sign-in"


HAND_ENTERED_NOTE = "public sign-in client entered by hand (not a secret)"


def offer_from_args(issuer_url: str, client_id: str) -> tuple[SiteOffer | None, str]:
    """A public client typed on the command line, or why it is refused."""
    issuer = str(issuer_url or "").strip()
    cid = str(client_id or "").strip()
    parts = urlsplit(issuer)
    if parts.scheme != "https" or not parts.hostname:
        return None, f"--sign-in-client needs an https issuer URL, not {issuer!r}"
    if not cid:
        return None, "--sign-in-client needs a client id after the issuer URL"
    return SiteOffer(site=bound_site_name() or parts.hostname, issuer_url=issuer, client_id=cid), ""


def record_offer(vault: Any, offer: SiteOffer, *, notes: str = "") -> SignInClient | None:
    """Record the site's public client in the vault and return it as a client.

    The vault keeps a value for every entry; a public client has no secret, so
    the value kept is the client id, which is public and is never read back.
    """
    name = entry_name(offer.site)
    vault.set(
        name,
        offer.client_id.encode("utf-8"),
        issuer_url=offer.issuer_url,
        client_id=offer.client_id,
        client_type="public",
        disposition="non_expiring",
        notes=notes or f"public sign-in client from the bound site {offer.site} (not a secret)",
    )
    return _client_from(
        {"name": name, "issuer_url": offer.issuer_url, "client_id": offer.client_id,
         "client_type": "public"}
    )


__all__ = [
    "NOT_ENABLED",
    "PREFIX",
    "SITE_SIGN_IN_PATH",
    "SignInClient",
    "SiteOffer",
    "bound_site_name",
    "choose",
    "detect",
    "entry_name",
    "from_bound_site",
    "HAND_ENTERED_NOTE",
    "is_public",
    "offer_from_args",
    "record_offer",
]
