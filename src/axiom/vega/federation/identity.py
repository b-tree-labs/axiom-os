# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Node identity — Ed25519 keypair, node_id, agent identity."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from axiom.infra.state_integrity import (
    StateDamaged,
    atomic_write_bytes,
    atomic_write_text,
    read_state_text,
)


def fingerprint(public_key_b64: str) -> str:
    """Human-comparable fingerprint of a public key.

    Returns SHA-256 of the raw pubkey as lowercase hex grouped by 4s —
    the user can read it aloud or compare character-by-character over a
    side channel (chat, phone) to confirm identity, Signal-style. This is
    the out-of-band verification step that closes TOFU.

    Example: ``c7a2 4f1b 93e8 2d11 ...``
    """
    try:
        raw = base64.b64decode(public_key_b64)
    except Exception:
        return ""
    digest = hashlib.sha256(raw).hexdigest()
    return " ".join(digest[i : i + 4] for i in range(0, len(digest), 4))


# ---------------------------------------------------------------------------
# Ed25519 helpers — prefer ``cryptography``; fall back to uuid-based stub.
# ---------------------------------------------------------------------------
_HAS_CRYPTO = False
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    _HAS_CRYPTO = True
except ImportError:  # pragma: no cover
    pass

_DEFAULT_KEYS_DIR = Path.home() / ".axi" / "identity"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NodeIdentity:
    """Globally unique node identity."""

    node_id: str  # SHA-256 of public key, first 16 hex chars
    public_key: str  # Base64-encoded Ed25519 public key
    private_key_path: Path  # Path to private key file (never transmitted)
    owner: str  # human_id (e.g., user@example.org)
    display_name: str  # human-friendly (e.g., "Ben's Workstation")
    profile: str = "standard"  # leaf, standard, provider, coordinator

    def to_manifest(self) -> dict:
        """Return fields suitable for inclusion in a :class:`NodeManifest`."""
        return {
            "node_id": self.node_id,
            "owner": self.owner,
            "display_name": self.display_name,
            "profile": self.profile,
            "public_key": self.public_key,
        }

    def agent_id(self, agent_type: str, version: str) -> str:
        """Generate agent identity: ``{owner}:{agent_type}:{version}``."""
        return f"{self.owner}:{agent_type}:{version}"


@dataclass
class NodeManifest:
    """Published at ``/.well-known/axiom-manifest.json``."""

    protocol_version: str = "0.1.0"
    node_id: str = ""
    owner: str = ""
    display_name: str = ""
    profile: str = "standard"
    capabilities: list[str] = field(default_factory=list)
    resources: dict = field(default_factory=dict)
    federation: dict = field(default_factory=dict)
    trust_level: str = "untrusted"
    extensions: list[dict] = field(default_factory=dict)
    axiom_version: str = ""
    active_generations: dict = field(default_factory=dict)  # {"rag-community": 3, "rag-org": 1}
    compatible_format_versions: list[str] = field(default_factory=lambda: ["1.0.0", "2.0.0"])

    def __post_init__(self) -> None:  # noqa: D105
        if not isinstance(self.extensions, list):
            self.extensions = list(self.extensions) if self.extensions else []

    def to_dict(self) -> dict:
        """Serialise to a plain ``dict``."""
        return {
            "protocol_version": self.protocol_version,
            "node_id": self.node_id,
            "owner": self.owner,
            "display_name": self.display_name,
            "profile": self.profile,
            "capabilities": self.capabilities,
            "resources": self.resources,
            "federation": self.federation,
            "trust_level": self.trust_level,
            "extensions": self.extensions,
            "axiom_version": self.axiom_version,
            "active_generations": self.active_generations,
            "compatible_format_versions": self.compatible_format_versions,
        }

    def to_json(self) -> str:
        """Serialise to a JSON string."""
        return json.dumps(self.to_dict(), indent=2)


# ---------------------------------------------------------------------------
# Key generation / persistence
# ---------------------------------------------------------------------------


def _derive_node_id(public_key_bytes: bytes) -> str:
    """SHA-256 of raw public key bytes, first 16 hex chars."""
    return hashlib.sha256(public_key_bytes).hexdigest()[:16]


class IdentityDamaged(StateDamaged):
    """The node identity on disk cannot be trusted or read. Never auto-replaced."""


def _identity_fix(keys_dir: Path) -> str:
    return (
        f"restore identity.json from a backup (the keypair in {keys_dir} is not touched); "
        f"only if you accept orphaning everything this node has signed, start over with "
        f"`mv '{keys_dir}' '{keys_dir}.old'` and then `axi federation init`"
    )


def generate_identity(
    owner: str,
    display_name: str = "",
    profile: str = "standard",
    keys_dir: Path | None = None,
    *,
    replace: bool = False,
) -> NodeIdentity:
    """Generate a new Ed25519 keypair and derive *node_id*.

    Uses the ``cryptography`` library when available.  Falls back to a
    hashlib-based UUID approach that is functional but cannot produce real
    cryptographic signatures.
    """
    keys_dir = keys_dir or _DEFAULT_KEYS_DIR
    keys_dir.mkdir(parents=True, exist_ok=True)

    existing = keys_dir / "private.pem"
    if not replace and os.path.lexists(existing):
        # A keypair already lives here. Minting a new one would overwrite the
        # only copy of the key every signature this node ever made verifies
        # against: the history would still be on disk and no longer checkable.
        # A missing or damaged identity.json is the usual way a caller arrives
        # here ("no identity, so make one"), which is exactly the wrong move.
        raise IdentityDamaged(
            existing,
            "already holds this node's private key, so a new identity was NOT generated "
            "(it would orphan everything signed with the old key)",
            _identity_fix(keys_dir),
        )

    if _HAS_CRYPTO:
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        pub_bytes = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
        priv_bytes = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    else:  # pragma: no cover — fallback for envs without cryptography
        pub_bytes = os.urandom(32)
        priv_bytes = os.urandom(64)

    pub_b64 = base64.b64encode(pub_bytes).decode()
    node_id = _derive_node_id(pub_bytes)

    priv_path = keys_dir / "private.pem"
    atomic_write_bytes(priv_path, priv_bytes, mode=0o600)

    pub_path = keys_dir / "public.b64"
    atomic_write_text(pub_path, pub_b64)

    display_name = display_name or f"{owner}-node"

    identity = NodeIdentity(
        node_id=node_id,
        public_key=pub_b64,
        private_key_path=priv_path,
        owner=owner,
        display_name=display_name,
        profile=profile,
    )
    save_identity(identity)
    return identity


def load_identity(keys_dir: Path | None = None) -> NodeIdentity | None:
    """Load existing identity from *keys_dir* (default ``~/.axi/identity/``).

    ``None`` means there is no identity AND no key: a clean slate. Anything
    else that is not a readable identity raises :class:`IdentityDamaged` naming
    the file and the repair. In particular a missing ``identity.json`` next to
    an existing ``private.pem`` is damage, not a clean slate: reporting it as
    ``None`` is what let callers mint a replacement and orphan signed history.
    """
    keys_dir = keys_dir or _DEFAULT_KEYS_DIR
    meta_path = keys_dir / "identity.json"
    fix = _identity_fix(keys_dir)
    try:
        text = read_state_text(meta_path, fix=fix)
    except StateDamaged as exc:
        raise IdentityDamaged(meta_path, exc.problem, fix) from None
    if text is None:
        if os.path.lexists(keys_dir / "private.pem"):
            raise IdentityDamaged(
                meta_path, "is missing but private.pem exists (identity half-erased)", fix
            )
        return None
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise TypeError(f"expected an object, found {type(data).__name__}")
        return NodeIdentity(
            node_id=str(data["node_id"]),
            public_key=str(data["public_key"]),
            private_key_path=Path(data["private_key_path"]),
            owner=str(data["owner"]),
            display_name=str(data["display_name"]),
            profile=data.get("profile", "standard"),
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise IdentityDamaged(
            meta_path, f"is not a valid identity record ({type(exc).__name__}: {exc})", fix
        ) from None


def save_identity(identity: NodeIdentity) -> None:
    """Persist identity metadata to ``identity.json`` alongside the keys."""
    keys_dir = identity.private_key_path.parent
    keys_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "node_id": identity.node_id,
        "public_key": identity.public_key,
        "private_key_path": str(identity.private_key_path),
        "owner": identity.owner,
        "display_name": identity.display_name,
        "profile": identity.profile,
    }
    atomic_write_text(keys_dir / "identity.json", json.dumps(meta, indent=2))
