# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Ed25519 signing primitives shared by ``axi ext sign`` and ``axi ext verify``.

This is the v0.1 local-signing backend. Silver conformance per spec §12.2
nominally requires Sigstore; for internal AEOS use we treat a locally-signed
ed25519 artifact (with a published-alongside attestation.json) as
Silver-equivalent. Real Sigstore keyless OIDC integration plugs in later via
a provider override — all the *verbs* stay put; only this module is swapped.

What's here:

- **Key handling**: generate an ed25519 keypair, load an existing one,
  compute the ``sha256`` of the public key for attestation pinning.
- **Artifact build**: tar up an extension's source tree (minus the exclusions
  from AEOS §13 / the playbook) into a reproducible tarball.
- **Sign**: compute a detached hex-encoded signature over the tarball bytes.
- **Verify**: re-check a signature against the public key, with clear
  messages when keys/artifacts are missing or tampered.
- **Trusted-store lookup**: ``$AXIOM_HOME/keys/trusted/<sha>.pub``.

The key files live at:

    $AXIOM_HOME/keys/signing-ed25519.pem      # private, mode 0600
    $AXIOM_HOME/keys/signing-ed25519.pub      # public
    $AXIOM_HOME/keys/trusted/<sha256>.pub     # pinned remote publishers
"""

from __future__ import annotations

import hashlib
import os
import stat
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from axiom.cli.ext.commands.config import _axiom_home

# ---------------------------------------------------------------------------
# Key filesystem layout
# ---------------------------------------------------------------------------


DEFAULT_PRIVATE_KEY_FILENAME = "signing-ed25519.pem"
DEFAULT_PUBLIC_KEY_FILENAME = "signing-ed25519.pub"


def keys_root() -> Path:
    return _axiom_home() / "keys"


def default_private_key_path() -> Path:
    return keys_root() / DEFAULT_PRIVATE_KEY_FILENAME


def default_public_key_path() -> Path:
    return keys_root() / DEFAULT_PUBLIC_KEY_FILENAME


def trusted_keys_dir() -> Path:
    return keys_root() / "trusted"


# Tarball exclusions. Anything matched here is skipped during build.
_ALWAYS_EXCLUDED_DIRNAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".venv",
        "venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".git",
    }
)

# Top-level dirs/files always excluded from release artifacts per the playbook.
_TOP_LEVEL_EXCLUSIONS: frozenset[str] = frozenset({"tests", "dist"})


# ---------------------------------------------------------------------------
# Key lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyPair:
    """An in-memory ed25519 keypair with filesystem path metadata."""

    private: ed25519.Ed25519PrivateKey
    public: ed25519.Ed25519PublicKey
    private_path: Path
    public_path: Path

    @property
    def public_key_bytes(self) -> bytes:
        """Return the serialized PEM form of the public key (for SHA pinning)."""
        return self.public.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    @property
    def public_key_sha256(self) -> str:
        return hashlib.sha256(self.public_key_bytes).hexdigest()


def generate_keypair(
    *, private_path: Path | None = None, public_path: Path | None = None
) -> KeyPair:
    """Generate a new keypair and persist it to disk with secure perms.

    Returns the :class:`KeyPair`. The private file is written with 0600
    perms (owner read/write only); the public file is world-readable.
    """
    private_path = private_path or default_private_key_path()
    public_path = public_path or default_public_key_path()
    private_path.parent.mkdir(parents=True, exist_ok=True)

    private = ed25519.Ed25519PrivateKey.generate()
    public = private.public_key()

    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Write private key with restrictive perms.
    # We ``os.open`` with O_WRONLY|O_CREAT|O_TRUNC so the 0600 mode is applied
    # at creation; chmod afterward is a no-op on a file that already exists
    # and would not close the umask race window on some platforms.
    fd = os.open(
        str(private_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(private_pem)
    except Exception:
        os.close(fd)
        raise
    # Belt-and-suspenders for platforms that ignored the mode at open().
    os.chmod(private_path, 0o600)

    public_path.write_bytes(public_pem)

    return KeyPair(
        private=private,
        public=public,
        private_path=private_path,
        public_path=public_path,
    )


def load_private_key(path: Path) -> ed25519.Ed25519PrivateKey:
    """Load an ed25519 private key from a PEM file."""
    data = path.read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError(
            f"key at {path} is not an ed25519 private key "
            f"(got {type(key).__name__})"
        )
    return key


def load_public_key(path: Path) -> ed25519.Ed25519PublicKey:
    """Load an ed25519 public key from a PEM file."""
    data = path.read_bytes()
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise ValueError(
            f"key at {path} is not an ed25519 public key "
            f"(got {type(key).__name__})"
        )
    return key


def public_key_sha256(pub_bytes: bytes) -> str:
    """Return the hex sha256 of the PEM-encoded public key bytes."""
    return hashlib.sha256(pub_bytes).hexdigest()


def load_keypair(
    private_path: Path | None = None,
    public_path: Path | None = None,
) -> KeyPair:
    """Load an existing keypair from disk.

    Raises :class:`FileNotFoundError` if either file is missing — callers that
    want auto-generation wrap this in their own flow (see ``axi ext sign``'s
    ``--yes`` handling).
    """
    private_path = private_path or default_private_key_path()
    public_path = public_path or default_public_key_path()
    priv = load_private_key(private_path)
    pub = load_public_key(public_path)
    return KeyPair(
        private=priv,
        public=pub,
        private_path=private_path,
        public_path=public_path,
    )


def check_private_key_permissions(path: Path) -> tuple[bool, str]:
    """Return ``(ok, note)`` for the private-key file's perms.

    On POSIX we want mode 0600; anything more permissive is an ``ok=False``.
    On platforms without meaningful POSIX perms (Windows), we skip the check.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False, f"could not stat {path}"
    if os.name != "posix":
        return True, f"mode check skipped on {os.name}"
    if mode & 0o077:
        return False, (
            f"private key {path} has permissive mode {oct(mode)}; "
            "run `chmod 0600 <path>` to fix"
        )
    return True, f"mode {oct(mode)}"


# ---------------------------------------------------------------------------
# Artifact tarball build
# ---------------------------------------------------------------------------


def _parse_gitignore(ext_path: Path) -> list[str]:
    """Return non-empty, non-comment patterns from ``.gitignore`` if present."""
    gitignore = ext_path / ".gitignore"
    if not gitignore.exists():
        return []
    patterns: list[str] = []
    for line in gitignore.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _gitignore_matches(rel: Path, patterns: list[str]) -> bool:
    """Very small fnmatch-based .gitignore matcher.

    We only honor simple glob and directory patterns — good enough for the
    typical extension (``dist/``, ``*.egg-info``, etc.). Full gitignore
    semantics (negation, ``**``, anchored vs unanchored) are deliberately out
    of scope for v0.1.
    """
    import fnmatch

    posix = rel.as_posix()
    for pat in patterns:
        p = pat.rstrip("/")
        if p.startswith("/"):
            p = p[1:]
        if fnmatch.fnmatch(posix, p) or fnmatch.fnmatch(rel.name, p):
            return True
        # Treat directory patterns as matching any child.
        if pat.endswith("/") and posix.startswith(p + "/"):
            return True
    return False


def build_artifact(ext_path: Path, dist_dir: Path | None = None) -> Path:
    """Build a ``tar.gz`` from ``ext_path`` and return the artifact path.

    Excludes: ``tests/``, ``dist/``, ``__pycache__/``, ``.venv/``,
    ``.pytest_cache/``, ``.mypy_cache/``, ``.ruff_cache/``, ``node_modules/``,
    ``.git/``, plus any simple patterns from ``.gitignore`` at the root.

    The output lives at ``<ext_path>/dist/<name>-<version>.tar.gz`` by
    default (AEOS authors expect ``dist/``). ``dist_dir`` overrides the
    output directory.
    """
    import tomllib

    manifest_path = ext_path / "axiom-extension.toml"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"axiom-extension.toml not found at {manifest_path}; "
            "cannot determine name + version for the artifact"
        )
    with manifest_path.open("rb") as fh:
        manifest = tomllib.load(fh)
    ext_block = manifest.get("extension", {})
    name = ext_block.get("name") or ext_path.name
    version = ext_block.get("version", "0.0.0")

    out_dir = dist_dir if dist_dir is not None else ext_path / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / f"{name}-{version}.tar.gz"

    gitignore_patterns = _parse_gitignore(ext_path)

    def _should_skip(rel: Path) -> bool:
        # Skip top-level excluded directories and files.
        if rel.parts and rel.parts[0] in _TOP_LEVEL_EXCLUSIONS:
            return True
        # Skip any always-excluded directory anywhere in the path.
        for part in rel.parts:
            if part in _ALWAYS_EXCLUDED_DIRNAMES:
                return True
        # Skip the dist/ output dir — don't self-include.
        if rel.parts and rel.parts[0] == "dist":
            return True
        # Skip gitignore matches (simple subset).
        if _gitignore_matches(rel, gitignore_patterns):
            return True
        return False

    # Build into a temp file and rename atomically so partial artifacts are
    # never visible to the caller. We open the tmp file with a plain file
    # object + ``tarfile.open(fileobj=...)`` so the gzip header does not
    # embed the randomized tmp filename — that would pollute byte-level
    # reproducibility.
    import gzip

    tmp_path = out_dir / f"{artifact_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        # Sort so the tarball is stable across builds (same inputs -> same
        # order); full bit-for-bit reproducibility would also need pinned
        # mtimes, which is out of scope for v0.1.
        files: list[Path] = []
        for p in sorted(ext_path.rglob("*"), key=lambda x: x.as_posix()):
            rel = p.relative_to(ext_path)
            if _should_skip(rel):
                continue
            files.append(p)

        with open(tmp_path, "wb") as raw_fh:
            # Pass an explicit filename to gzip so the header is stable and
            # independent of the tmpfile's path on disk.
            with gzip.GzipFile(
                filename=artifact_path.name, fileobj=raw_fh, mode="wb"
            ) as gz:
                with tarfile.open(fileobj=gz, mode="w") as tar:
                    for p in files:
                        rel = p.relative_to(ext_path)
                        arcname = f"{name}-{version}/{rel.as_posix()}"
                        tar.add(p, arcname=arcname, recursive=False)
        os.replace(tmp_path, artifact_path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    return artifact_path


# ---------------------------------------------------------------------------
# Hashing + signing
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """Stream-hash a file and return the hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sign_bytes(private_key: ed25519.Ed25519PrivateKey, data: bytes) -> bytes:
    return private_key.sign(data)


def sign_file(private_key: ed25519.Ed25519PrivateKey, path: Path) -> str:
    """Sign ``path`` and return the signature as a hex string."""
    raw = path.read_bytes()
    sig = sign_bytes(private_key, raw)
    return sig.hex()


def verify_bytes(
    public_key: ed25519.Ed25519PublicKey, data: bytes, signature_hex: str
) -> bool:
    try:
        public_key.verify(bytes.fromhex(signature_hex), data)
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_file(
    public_key: ed25519.Ed25519PublicKey, path: Path, signature_hex: str
) -> bool:
    """Verify ``path`` was signed under ``public_key`` with ``signature_hex``."""
    return verify_bytes(public_key, path.read_bytes(), signature_hex)


__all__ = [
    "DEFAULT_PRIVATE_KEY_FILENAME",
    "DEFAULT_PUBLIC_KEY_FILENAME",
    "KeyPair",
    "build_artifact",
    "check_private_key_permissions",
    "default_private_key_path",
    "default_public_key_path",
    "generate_keypair",
    "keys_root",
    "load_keypair",
    "load_private_key",
    "load_public_key",
    "public_key_sha256",
    "sha256_file",
    "sign_bytes",
    "sign_file",
    "trusted_keys_dir",
    "verify_bytes",
    "verify_file",
]
