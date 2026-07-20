# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Ported from SoilMetrix, Inc (dba Aiterra.ag) by Benjamin Booth, CEO.

"""ES256 signing keys + JWKS for webauth session tokens (ADR-085).

Why asymmetric, and why *not* the vega identity key: an Axiom node that issues
tokens third parties verify (MCP clients, resource servers) must sign with a key
whose *private* half never leaves the issuer. HS256 fails this — every verifier
would hold the forging secret. So webauth signs with **ES256 (ECDSA P-256)** and
publishes only the public half as a JWK Set. It is deliberately a *separate* key
from the node's Ed25519 vega identity (ADR-022): reusing that key would couple
OAuth ``kid`` rotation to federation-identity rotation, and EdDSA is not
universally verifiable by third-party JOSE stacks.

Keys carry a ``kid`` equal to their RFC 7638 JWK thumbprint, so rotation is a
matter of adding a new active key while retired keys stay in the JWKS for
verification until their tokens age out (overlap validity, ADR-080). Private
keys load from the Axiom secrets provider; a dev/test process mints an ephemeral
key so ``import axiom.webauth`` stays side-effect-free and no key is required to
run tests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from axiom.setup.secrets import get_secret

_ALG = "ES256"
_CRV = "P-256"
_KTY = "EC"
#: P-256 field elements are 32 bytes; JWK x/y are the fixed-width big-endian
#: coordinates, base64url-encoded (RFC 7518 §6.2.1.2).
_COORD_BYTES = 32

_DEV_ENVIRONMENTS = {"development", "test"}


def _b64u(raw: bytes) -> str:
    """base64url without padding, per RFC 7515 §2 / JOSE convention."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _thumbprint(x: str, y: str) -> str:
    """RFC 7638 JWK thumbprint for an EC key.

    The required members for ``kty=EC`` are ``crv``, ``kty``, ``x``, ``y`` in
    lexicographic order with no whitespace; SHA-256, base64url, no padding.
    """
    canonical = json.dumps(
        {"crv": _CRV, "kty": _KTY, "x": x, "y": y},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return _b64u(hashlib.sha256(canonical).digest())


class SigningKey:
    """An ES256 signing key with its RFC 7638 ``kid`` and public JWK."""

    def __init__(
        self,
        private_key: ec.EllipticCurvePrivateKey,
        alg: str = _ALG,
        kid: str | None = None,
    ) -> None:
        if not isinstance(private_key, ec.EllipticCurvePrivateKey):
            raise TypeError("SigningKey requires an EC private key (P-256 / ES256).")
        self._private = private_key
        self.alg = alg
        nums = private_key.public_key().public_numbers()
        self._x = _b64u(nums.x.to_bytes(_COORD_BYTES, "big"))
        self._y = _b64u(nums.y.to_bytes(_COORD_BYTES, "big"))
        #: kid defaults to the key's own thumbprint (stable, collision-resistant).
        self.kid = kid or _thumbprint(self._x, self._y)

    @classmethod
    def generate(cls, alg: str = _ALG) -> SigningKey:
        return cls(ec.generate_private_key(ec.SECP256R1()), alg)

    @classmethod
    def from_pem(
        cls, pem: str | bytes, alg: str = _ALG, kid: str | None = None
    ) -> SigningKey:
        if isinstance(pem, str):
            pem = pem.encode("utf-8")
        return cls(serialization.load_pem_private_key(pem, password=None), alg, kid)

    def to_pem(self) -> str:
        return self._private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")

    def public_jwk(self) -> dict:
        """Public JWK — safe to publish; never carries the private scalar ``d``."""
        return {
            "kty": _KTY,
            "crv": _CRV,
            "x": self._x,
            "y": self._y,
            "use": "sig",
            "alg": self.alg,
            "kid": self.kid,
        }

    @property
    def signing_key(self) -> ec.EllipticCurvePrivateKey:
        """The private key object PyJWT signs with."""
        return self._private

    @property
    def verifying_key(self) -> ec.EllipticCurvePublicKey:
        """The public key object PyJWT verifies with."""
        return self._private.public_key()


class KeyStore:
    """The active signing key plus any retired (verify-only) keys.

    Rotation adds a new active key; the old key stays in the store — and thus in
    the JWKS — so tokens it signed keep verifying until they expire.
    """

    def __init__(self, keys: list[SigningKey], active_kid: str) -> None:
        self._keys = {k.kid: k for k in keys}
        if active_kid not in self._keys:
            raise ValueError(f"active_kid {active_kid!r} is not among the provided keys")
        self._active = active_kid

    @property
    def active(self) -> SigningKey:
        return self._keys[self._active]

    def get(self, kid: str | None) -> SigningKey | None:
        if kid is None:
            return None
        return self._keys.get(kid)

    def jwks(self) -> dict:
        """RFC 7517 JWK Set of every public key — served at the JWKS endpoint."""
        return {"keys": [k.public_jwk() for k in self._keys.values()]}


_store: KeyStore | None = None


def load_key_store() -> KeyStore:
    """Build the process key store from the secrets provider (or dev-ephemeral).

    Resolution: ``WEBAUTH_SIGNING_KEY`` (a PKCS8 PEM) via env → Axiom secrets
    provider, with optional ``WEBAUTH_RETIRED_KEYS`` (a JSON array of PEMs) kept
    verify-only during rotation. With no configured key, a dev/test process mints
    an ephemeral key; any other ``ENVIRONMENT`` fail-closes rather than silently
    signing with a key that dies on the next replica or restart.
    """
    pem = os.getenv("WEBAUTH_SIGNING_KEY") or get_secret("WEBAUTH_SIGNING_KEY")
    if pem:
        active = SigningKey.from_pem(pem)
        keys = [active]
        retired = os.getenv("WEBAUTH_RETIRED_KEYS") or get_secret("WEBAUTH_RETIRED_KEYS")
        if retired:
            for retired_pem in json.loads(retired):
                keys.append(SigningKey.from_pem(retired_pem))
        return KeyStore(keys, active.kid)

    environment = os.getenv("ENVIRONMENT", "development")
    if environment in _DEV_ENVIRONMENTS:
        active = SigningKey.generate()
        return KeyStore([active], active.kid)

    raise RuntimeError(
        "WEBAUTH_SIGNING_KEY must be set in deployed environments "
        f"(ENVIRONMENT={environment!r}); refusing to sign with an ephemeral key."
    )


def get_key_store() -> KeyStore:
    """Return the cached process key store, building it on first use."""
    global _store
    if _store is None:
        _store = load_key_store()
    return _store


def reset_key_store_for_tests() -> None:
    """Drop the cached key store (test isolation for rotation/env scenarios)."""
    global _store
    _store = None
