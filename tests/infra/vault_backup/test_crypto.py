# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The archive format: authenticated, key-bound, and honest about failure."""

from __future__ import annotations

import json
import struct

import pytest

from axiom.infra.vault_backup import crypto as C

from .conftest import CANARY_A, PASSPHRASE

# Fast KDF for tests; production defaults are asserted separately.
FAST = {"n": 2**10, "r": 8, "p": 1}


@pytest.fixture
def recipient():
    code = C.generate_recovery_code()
    return C.create_recipient(code, kdf=FAST), code


def _seal(recipient, entries=None):
    entries = entries if entries is not None else {"a": {"kind": "foreign", "value": CANARY_A.encode()}}
    manifest = [{"name": k, "kind": v["kind"], "fingerprint": "f" * 16} for k, v in entries.items()]
    return C.seal(
        entries, {"id": "vb-1", "created_at": "2026-01-01T00:00:00.000000Z", "host": "h", "entries": manifest},
        recipient=recipient,
    )


def test_recovery_code_has_high_entropy_and_a_checksum():
    codes = {C.generate_recovery_code() for _ in range(50)}
    assert len(codes) == 50
    code = next(iter(codes))
    assert C.parse_recovery_code(code) == C.parse_recovery_code(code.lower().replace("-", " "))
    flipped = code[:2] + ("A" if code[2] != "A" else "B") + code[3:]
    with pytest.raises(C.RecoveryCodeError):
        C.parse_recovery_code(flipped)


def test_recovery_code_carries_at_least_128_bits():
    assert C.RECOVERY_CODE_BITS >= 128


def test_round_trip_with_the_recovery_code(recipient):
    rec, code = recipient
    sealed = _seal(rec)
    header, body = C.open_archive(sealed.archive, unlock=code)
    assert body["entries"]["a"]["value"] == CANARY_A.encode()
    assert header["count"] == 1 or header["id"] == "vb-1"


def test_no_value_in_cleartext(recipient):
    import base64

    rec, _ = recipient
    sealed = _seal(rec)
    assert CANARY_A.encode() not in sealed.archive
    assert base64.b64encode(CANARY_A.encode()) not in sealed.archive
    assert CANARY_A not in json.dumps(C.read_header(sealed.archive)[0])


def test_sender_can_verify_without_the_private_key(recipient):
    """Verify-on-write: the writer holds the ephemeral secret, not the recipient's."""
    rec, _ = recipient
    sealed = _seal(rec)
    body = C.open_with_content_key(sealed.archive, sealed.content_key)
    assert body["entries"]["a"]["value"] == CANARY_A.encode()


def test_wrong_code_is_a_generic_error(recipient):
    rec, _ = recipient
    sealed = _seal(rec)
    with pytest.raises(C.DecryptionError) as e:
        C.open_archive(sealed.archive, unlock=C.generate_recovery_code())
    assert "wrong key or damaged" in str(e.value)
    assert CANARY_A not in str(e.value)


@pytest.mark.parametrize("where", ["header_entry", "ciphertext", "nonce", "wrapped_key"])
def test_any_tamper_fails_with_the_same_generic_error(recipient, where):
    rec, code = recipient
    data = bytearray(_seal(rec).archive)
    header, hbytes, off = C.read_header(bytes(data))
    if where == "ciphertext":
        data[-5] ^= 1
    else:
        h = json.loads(hbytes)
        if where == "header_entry":
            h["entries"][0]["name"] = "evil"
        elif where == "nonce":
            h["enc"]["nonce"] = C._b64e(b"\x01" * 12)
        else:
            h["enc"]["wrapped_priv"]["ct"] = C._b64e(b"\x02" * 48)
        nh = C._canon(h)
        data = bytearray(C.MAGIC + struct.pack(">I", len(nh)) + nh + bytes(data[off:]))
    with pytest.raises(C.DecryptionError) as e:
        C.open_archive(bytes(data), unlock=code)
    assert str(e.value) == C.GENERIC_FAILURE


def test_truncated_and_foreign_files_are_format_errors_not_crypto_errors(recipient):
    rec, code = recipient
    data = _seal(rec).archive
    for bad in (b"", b"not an archive", data[:10], data[: len(data) // 2][:20]):
        with pytest.raises(C.FormatError):
            C.open_archive(bad, unlock=code)


def test_truncated_ciphertext_fails_authentication(recipient):
    rec, code = recipient
    data = _seal(rec).archive
    with pytest.raises(C.DecryptionError):
        C.open_archive(data[:-3], unlock=code)


def test_a_newer_format_is_refused_with_a_version_message(recipient):
    rec, code = recipient
    data = _seal(rec).archive
    header, hbytes, off = C.read_header(data)
    h = json.loads(hbytes)
    h["format"] = C.FORMAT_VERSION + 1
    nh = C._canon(h)
    newer = C.MAGIC + struct.pack(">I", len(nh)) + nh + data[off:]
    with pytest.raises(C.UnsupportedVersion) as e:
        C.open_archive(newer, unlock=code)
    assert "newer" in str(e.value)


def test_absurd_kdf_parameters_are_refused_before_any_work(recipient):
    rec, code = recipient
    data = _seal(rec).archive
    header, hbytes, off = C.read_header(data)
    h = json.loads(hbytes)
    h["enc"]["wrapped_priv"]["n"] = 2**30
    nh = C._canon(h)
    evil = C.MAGIC + struct.pack(">I", len(nh)) + nh + data[off:]
    with pytest.raises(C.FormatError):
        C.open_archive(evil, unlock=code)


def test_passphrase_mode_round_trip_and_minimum_length():
    entries = {"a": {"kind": "foreign", "value": b"v"}}
    sealed = C.seal(entries, {"id": "x", "entries": []}, passphrase=PASSPHRASE, kdf=FAST)
    _, body = C.open_archive(sealed.archive, unlock=PASSPHRASE)
    assert body["entries"]["a"]["value"] == b"v"
    with pytest.raises(C.DecryptionError):
        C.open_archive(sealed.archive, unlock="x" * 20)
    with pytest.raises(C.WeakPassphrase):
        C.seal(entries, {"id": "x", "entries": []}, passphrase="short", kdf=FAST)


def test_user_chosen_passphrase_can_wrap_the_recipient_key():
    rec = C.create_recipient(PASSPHRASE, kdf=FAST, label="passphrase")
    sealed = _seal(rec)
    C.open_archive(sealed.archive, unlock=PASSPHRASE)
    with pytest.raises(C.WeakPassphrase):
        C.create_recipient("short", kdf=FAST, label="passphrase")


def test_default_kdf_is_memory_hard():
    assert C.DEFAULT_KDF["n"] * C.DEFAULT_KDF["r"] * 128 >= 32 * 1024 * 1024


def test_each_seal_uses_fresh_randomness(recipient):
    rec, _ = recipient
    a, b = _seal(rec), _seal(rec)
    assert a.archive != b.archive and a.content_key != b.content_key


def test_the_recipient_record_never_holds_the_private_key_in_the_clear(recipient):
    rec, _ = recipient
    blob = json.dumps(rec.to_dict())
    priv = C.unwrap_private(rec.wrapped_priv, rec.public, recipient[1])
    from cryptography.hazmat.primitives import serialization as s

    raw = priv.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())
    assert C._b64e(raw) not in blob and raw.hex() not in blob
