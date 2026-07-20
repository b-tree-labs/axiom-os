# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Node identity — Ed25519 keypair, node_id, agent identity."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


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


def generate_identity(
    owner: str,
    display_name: str = "",
    profile: str = "standard",
    keys_dir: Path | None = None,
) -> NodeIdentity:
    """Generate a new Ed25519 keypair and derive *node_id*.

    Uses the ``cryptography`` library when available.  Falls back to a
    hashlib-based UUID approach that is functional but cannot produce real
    cryptographic signatures.
    """
    keys_dir = keys_dir or _DEFAULT_KEYS_DIR
    keys_dir.mkdir(parents=True, exist_ok=True)

    if _HAS_CRYPTO:
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        pub_bytes = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
        priv_bytes = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    else:  # pragma: no cover — fallback for envs without cryptography
        import os

        pub_bytes = os.urandom(32)
        priv_bytes = os.urandom(64)

    pub_b64 = base64.b64encode(pub_bytes).decode()
    node_id = _derive_node_id(pub_bytes)

    priv_path = keys_dir / "private.pem"
    priv_path.write_bytes(priv_bytes)
    priv_path.chmod(0o600)

    pub_path = keys_dir / "public.b64"
    pub_path.write_text(pub_b64)

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
    """Load existing identity from *keys_dir* (default ``~/.axi/identity/``)."""
    keys_dir = keys_dir or _DEFAULT_KEYS_DIR
    meta_path = keys_dir / "identity.json"
    if not meta_path.exists():
        return None
    data = json.loads(meta_path.read_text())
    return NodeIdentity(
        node_id=data["node_id"],
        public_key=data["public_key"],
        private_key_path=Path(data["private_key_path"]),
        owner=data["owner"],
        display_name=data["display_name"],
        profile=data.get("profile", "standard"),
    )


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
    (keys_dir / "identity.json").write_text(json.dumps(meta, indent=2))
