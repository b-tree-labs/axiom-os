# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Archive format and cryptography for vault backups.

No home-rolled primitives: everything is a vetted ``cryptography`` building
block composed the way age composes them.

* Content: AES-256-GCM.  The whole cleartext header (version, id, host, the
  per-entry name / fingerprint list, the KDF and key-wrap parameters, the
  nonce) is the AAD, so changing any byte of it fails authentication.
* Recipient mode (the default, and the only one a scheduled run can use): a
  fresh X25519 ephemeral key per archive, ECDH with the recipient's PUBLIC key,
  HKDF-SHA256 to the content key.  Backing up needs no secret.  The recipient
  PRIVATE key is wrapped with scrypt + AES-GCM under the recovery code (or a
  user-chosen passphrase) and the wrapped form rides in every archive, so one
  archive plus the code is sufficient to restore even when the state dir is
  gone.  The code is never stored anywhere.
* Passphrase mode: scrypt straight to the content key.
* Verify-on-write: the writer knows the ephemeral secret, so it can re-derive
  the content key and decrypt what it just wrote without the recipient's
  private key.

Failure policy: once the file parses, every cryptographic failure (wrong code,
flipped bit, swapped header, short ciphertext) raises the same
:class:`DecryptionError` with the same text after the same work, so the error
is not an oracle for which part was wrong.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
import unicodedata
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"AXVB\x01\n"
FORMAT_VERSION = 1
MAX_HEADER_BYTES = 16 * 1024 * 1024

#: scrypt N=2^15, r=8, p=1: 32 MiB of memory per guess.
DEFAULT_KDF: dict[str, int] = {"n": 2**15, "r": 8, "p": 1}
_MAX_KDF_MEMORY = 256 * 1024 * 1024

RECOVERY_CODE_BYTES = 20
RECOVERY_CODE_BITS = RECOVERY_CODE_BYTES * 8
MIN_PASSPHRASE_CHARS = 12

GENERIC_FAILURE = "cannot decrypt: wrong key or damaged backup"

_INFO_CONTENT = b"axiom-vault-backup/v1/content"
_AAD_WRAP = b"axiom-vault-backup/v1/wrap"


class VaultBackupError(RuntimeError):
    """Base class for vault-backup failures."""


class FormatError(VaultBackupError):
    """Not a vault backup, or structurally damaged. Independent of any key."""


class UnsupportedVersion(FormatError):
    """Written by a newer version than this one understands."""


class DecryptionError(VaultBackupError):
    """The one error for every cryptographic failure."""

    def __init__(self) -> None:
        super().__init__(GENERIC_FAILURE)


class WeakPassphrase(VaultBackupError):
    pass


class RecoveryCodeError(VaultBackupError):
    pass


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    try:
        return base64.b64decode(s.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise FormatError("backup header is damaged") from exc


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


# -- recovery code ---------------------------------------------------------


def _check(chars: str) -> str:
    d = hashlib.sha256(b"axiom-vault-backup/recovery-check/" + chars.encode()).digest()
    return base64.b32encode(d[:3]).decode().rstrip("=")[:4]


def generate_recovery_code() -> str:
    """160 random bits as 32 base32 characters + a 4 character typo check.

    ``XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-CCCC``.  Shown once, never stored.
    """
    chars = base64.b32encode(secrets.token_bytes(RECOVERY_CODE_BYTES)).decode().rstrip("=")
    full = chars + _check(chars)
    return "-".join(full[i : i + 4] for i in range(0, len(full), 4))


def parse_recovery_code(text: str) -> str:
    """Normalise what a person typed; raise on a typo (the checksum, not a key test)."""
    flat = "".join(ch for ch in text.upper() if ch.isalnum())
    if len(flat) != 36 or _check(flat[:32]) != flat[32:]:
        raise RecoveryCodeError("that does not look like a recovery code (typo?)")
    return flat


def looks_like_recovery_code(text: str) -> bool:
    try:
        parse_recovery_code(text)
        return True
    except RecoveryCodeError:
        return False


def _unlock_material(unlock: str, label: str) -> bytes:
    if label == "recovery-code":
        try:
            return parse_recovery_code(unlock).encode()
        except RecoveryCodeError:
            # Same path as a wrong secret: no checksum oracle at decrypt time.
            return b"\x00invalid-recovery-code\x00" + unlock.encode("utf-8", "replace")
    return unicodedata.normalize("NFKC", unlock).encode("utf-8")


def _check_passphrase(p: str) -> None:
    if len(p) < MIN_PASSPHRASE_CHARS:
        raise WeakPassphrase(f"passphrase must be at least {MIN_PASSPHRASE_CHARS} characters")


# -- KDF / wrapping --------------------------------------------------------


def _validate_kdf(k: Any) -> tuple[int, int, int]:
    try:
        n, r, p = int(k["n"]), int(k["r"]), int(k["p"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FormatError("backup header is damaged") from exc
    ok = (
        2**10 <= n <= 2**22 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 8
        and 128 * n * r <= _MAX_KDF_MEMORY
    )
    if not ok:
        raise FormatError("backup header has unreasonable key-derivation parameters")
    return n, r, p


def _scrypt(material: bytes, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(material)


def _private_raw(key: X25519PrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())


def _public_raw(key: X25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@dataclass(frozen=True)
class Recipient:
    """The public half, plus the private half wrapped under the recovery secret."""

    public: bytes
    wrapped_priv: dict[str, Any]
    label: str
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "public": _b64e(self.public), "wrapped_priv": self.wrapped_priv,
            "label": self.label, "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Recipient:
        try:
            return cls(public=_b64d(d["public"]), wrapped_priv=dict(d["wrapped_priv"]),
                       label=str(d["label"]), created_at=str(d.get("created_at", "")))
        except (KeyError, TypeError) as exc:
            raise FormatError("recipient file is damaged") from exc

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.public).hexdigest()[:16]


def create_recipient(unlock: str, *, label: str = "recovery-code",
                     kdf: dict[str, int] | None = None, created_at: str = "") -> Recipient:
    """New X25519 recipient key, private half wrapped under ``unlock``."""
    if label == "passphrase":
        _check_passphrase(unlock)
    elif label != "recovery-code":
        raise ValueError("label must be 'recovery-code' or 'passphrase'")
    kdf = dict(kdf or DEFAULT_KDF)
    n, r, p = _validate_kdf(kdf)
    priv = X25519PrivateKey.generate()
    pub = _public_raw(priv.public_key())
    salt, nonce = os.urandom(16), os.urandom(12)
    kek = _scrypt(_unlock_material(unlock, label), salt, n, r, p)
    ct = AESGCM(kek).encrypt(nonce, _private_raw(priv), _AAD_WRAP + pub)
    wrapped = {"kdf": "scrypt", "n": n, "r": r, "p": p, "label": label,
               "salt": _b64e(salt), "nonce": _b64e(nonce), "ct": _b64e(ct)}
    return Recipient(public=pub, wrapped_priv=wrapped, label=label, created_at=created_at)


def unwrap_private(wrapped: dict[str, Any], public: bytes, unlock: str) -> X25519PrivateKey:
    try:
        n, r, p = _validate_kdf(wrapped)
        label = str(wrapped.get("label", ""))
        if label not in ("recovery-code", "passphrase"):
            raise FormatError("backup header is damaged")
        salt, nonce, ct = _b64d(wrapped["salt"]), _b64d(wrapped["nonce"]), _b64d(wrapped["ct"])
    except KeyError as exc:
        raise FormatError("backup header is damaged") from exc
    kek = _scrypt(_unlock_material(unlock, label), salt, n, r, p)
    try:
        raw = AESGCM(kek).decrypt(nonce, ct, _AAD_WRAP + public)
        return X25519PrivateKey.from_private_bytes(raw)
    except (InvalidTag, ValueError):
        raise DecryptionError() from None


# -- seal / open -----------------------------------------------------------


@dataclass(frozen=True)
class Sealed:
    archive: bytes
    #: In-memory only, for verify-on-write.  Never persisted or returned to callers beyond that.
    content_key: bytes


def _derive_content_key(shared: bytes, eph_pub: bytes, rec_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub + rec_pub, info=_INFO_CONTENT).derive(shared)


def _frame(header: dict[str, Any], ct: bytes) -> bytes:
    hb = _canon(header)
    return MAGIC + struct.pack(">I", len(hb)) + hb + ct


def seal(
    entries: dict[str, dict[str, Any]],
    header_fields: dict[str, Any],
    *,
    recipient: Recipient | None = None,
    passphrase: str | None = None,
    kdf: dict[str, int] | None = None,
) -> Sealed:
    """Encrypt ``entries`` (name -> {"kind", "value": bytes, ...}) into one archive."""
    if (recipient is None) == (passphrase is None):
        raise ValueError("give exactly one of recipient or passphrase")
    nonce = os.urandom(12)
    header: dict[str, Any] = {k: v for k, v in header_fields.items() if k != "enc"}
    header["format"] = FORMAT_VERSION
    header["count"] = len(entries)
    enc: dict[str, Any] = {"alg": "AES-256-GCM", "nonce": _b64e(nonce)}
    if recipient is not None:
        eph = X25519PrivateKey.generate()
        eph_pub = _public_raw(eph.public_key())
        key = _derive_content_key(eph.exchange(X25519PublicKey.from_public_bytes(recipient.public)),
                                  eph_pub, recipient.public)
        enc.update({"mode": "recipient", "kem": "X25519-HKDF-SHA256", "eph_pub": _b64e(eph_pub),
                    "recipient_pub": _b64e(recipient.public), "recipient_fp": recipient.fingerprint,
                    "wrapped_priv": recipient.wrapped_priv})
    else:
        assert passphrase is not None
        _check_passphrase(passphrase)
        k = dict(kdf or DEFAULT_KDF)
        n, r, p = _validate_kdf(k)
        salt = os.urandom(16)
        key = _scrypt(_unlock_material(passphrase, "passphrase"), salt, n, r, p)
        enc.update({"mode": "passphrase", "kdf": "scrypt", "n": n, "r": r, "p": p, "salt": _b64e(salt)})
    header["enc"] = enc
    plaintext = _canon({"v": 1, "entries": {
        name: {**{k: v for k, v in e.items() if k != "value"}, "value": _b64e(e["value"])}
        for name, e in entries.items()
    }})
    prefix = MAGIC + struct.pack(">I", len(_canon(header))) + _canon(header)
    ct = AESGCM(key).encrypt(nonce, plaintext, prefix)
    return Sealed(archive=prefix + ct, content_key=key)


def read_header(data: bytes) -> tuple[dict[str, Any], bytes, int]:
    """Parse the cleartext header: ``(header, header_bytes, ciphertext_offset)``. No key needed."""
    if len(data) < len(MAGIC) + 4 or not data.startswith(MAGIC[:4]):
        raise FormatError("not a vault backup")
    if not data.startswith(MAGIC):
        raise FormatError("not a vault backup, or written by an unknown format revision")
    (hlen,) = struct.unpack(">I", data[len(MAGIC) : len(MAGIC) + 4])
    start = len(MAGIC) + 4
    if hlen > MAX_HEADER_BYTES or start + hlen > len(data):
        raise FormatError("backup is truncated or damaged")
    hb = data[start : start + hlen]
    try:
        header = json.loads(hb)
        fmt = int(header["format"])
        enc = header["enc"]
        mode = enc["mode"]
    except (ValueError, KeyError, TypeError) as exc:
        raise FormatError("backup header is damaged") from exc
    if fmt > FORMAT_VERSION:
        raise UnsupportedVersion(
            f"this backup was written by a newer version (format {fmt}; this build reads up to "
            f"{FORMAT_VERSION}). Upgrade, or use the standalone restore script from the newer release."
        )
    if mode == "recipient":
        _validate_kdf(enc.get("wrapped_priv") or {})
    elif mode == "passphrase":
        _validate_kdf(enc)
    else:
        raise FormatError("backup header is damaged")
    return header, hb, start + hlen


def _decode_plaintext(pt: bytes) -> dict[str, Any]:
    try:
        body = json.loads(pt)
        entries = {
            name: {**{k: v for k, v in e.items() if k != "value"}, "value": base64.b64decode(e["value"])}
            for name, e in body["entries"].items()
        }
    except Exception:  # noqa: BLE001
        raise DecryptionError() from None
    return {"v": body.get("v", 1), "entries": entries}


def _open(data: bytes, derive) -> tuple[dict[str, Any], dict[str, Any]]:
    header, hb, off = read_header(data)
    try:
        nonce = _b64d(header["enc"]["nonce"])
        key = derive(header)
        pt = AESGCM(key).decrypt(nonce, data[off:], data[:off])
    except (InvalidTag, ValueError, KeyError):
        raise DecryptionError() from None
    return header, _decode_plaintext(pt)


def open_archive(data: bytes, *, unlock: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decrypt with the recovery code / passphrase. Returns ``(header, body)``."""

    def derive(header: dict[str, Any]) -> bytes:
        enc = header["enc"]
        if enc["mode"] == "recipient":
            pub = _b64d(enc["recipient_pub"])
            priv = unwrap_private(enc["wrapped_priv"], pub, unlock)
            eph_pub = _b64d(enc["eph_pub"])
            return _derive_content_key(priv.exchange(X25519PublicKey.from_public_bytes(eph_pub)), eph_pub, pub)
        n, r, p = _validate_kdf(enc)
        return _scrypt(_unlock_material(unlock, "passphrase"), _b64d(enc["salt"]), n, r, p)

    return _open(data, derive)


def open_with_content_key(data: bytes, key: bytes) -> dict[str, Any]:
    """Decrypt with the in-memory content key (verify-on-write)."""
    _, body = _open(data, lambda _h: key)
    return body
