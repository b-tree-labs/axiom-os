# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the JSON-file-backed user store + its loader/writer.

Covers the two properties the gate depends on: (1) fail-closed validation of
the accounts file (a bad file yields *no* accounts, never a partial set), and
(2) mtime-triggered hot-reload — a running store picks up an appended account
on the next lookup, no process restart. The reload behaviour mirrors the
consumer's role index so one edited file feeds both auth and role resolution.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from axiom.webauth.file_store import (
    AccountsFileError,
    JsonFileUserStore,
    load_user_records,
    save_user_records,
    upsert_user_record,
)
from axiom.webauth.password import get_password_hash
from axiom.webauth.users import authenticate

PW = "Correct-Horse-9"


def _write(path: Path, records: list[dict]) -> Path:
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def _rec(email: str, roles=("operator",), name="A. User") -> dict:
    return {
        "email": email,
        "password_hash": get_password_hash(PW),
        "name": name,
        "roles": list(roles),
    }


# ---------- load_user_records: happy path + normalization -----------------


def test_load_normalizes(tmp_path: Path):
    f = _write(tmp_path / "u.json", [
        {"email": "Op@UT.Example", "password_hash": "scrypt$x", "roles": ["operator"]},
    ])
    recs = load_user_records(f)
    assert len(recs) == 1
    r = recs[0]
    assert r["email"] == "op@ut.example"          # lower-cased
    assert r["user_id"] == "op@ut.example"        # defaults to email
    assert r["roles"] == ("operator",)            # tuple
    assert r["name"] == ""                         # defaulted


def test_load_keeps_explicit_user_id(tmp_path: Path):
    f = _write(tmp_path / "u.json", [
        {"user_id": "op-20", "email": "op@ut.example",
         "password_hash": "scrypt$x", "roles": ["operator"]},
    ])
    assert load_user_records(f)[0]["user_id"] == "op-20"


def test_load_empty_list_is_ok(tmp_path: Path):
    f = _write(tmp_path / "u.json", [])
    assert load_user_records(f) == []


# ---------- load_user_records: fail-closed validation ---------------------


def test_load_missing_file_raises(tmp_path: Path):
    with pytest.raises(AccountsFileError):
        load_user_records(tmp_path / "nope.json")


def test_load_top_level_must_be_list(tmp_path: Path):
    f = (tmp_path / "u.json")
    f.write_text('{"accounts": []}', encoding="utf-8")
    with pytest.raises(AccountsFileError):
        load_user_records(f)


def test_load_rejects_plaintext_password(tmp_path: Path):
    f = _write(tmp_path / "u.json", [
        {"email": "a@b.c", "password": "hunter2", "roles": []},
    ])
    with pytest.raises(AccountsFileError) as ei:
        load_user_records(f)
    assert "password_hash" in str(ei.value)


def test_load_rejects_unknown_field(tmp_path: Path):
    f = _write(tmp_path / "u.json", [
        {"email": "a@b.c", "password_hash": "scrypt$x", "admin": True},
    ])
    with pytest.raises(AccountsFileError):
        load_user_records(f)


def test_load_requires_email_and_hash(tmp_path: Path):
    f1 = _write(tmp_path / "a.json", [{"password_hash": "scrypt$x"}])
    with pytest.raises(AccountsFileError):
        load_user_records(f1)
    f2 = _write(tmp_path / "b.json", [{"email": "a@b.c"}])
    with pytest.raises(AccountsFileError):
        load_user_records(f2)


def test_load_rejects_duplicate_email(tmp_path: Path):
    f = _write(tmp_path / "u.json", [
        {"email": "a@b.c", "password_hash": "scrypt$x"},
        {"email": "A@B.C", "password_hash": "scrypt$y"},
    ])
    with pytest.raises(AccountsFileError):
        load_user_records(f)


def test_load_roles_must_be_list(tmp_path: Path):
    f = _write(tmp_path / "u.json", [
        {"email": "a@b.c", "password_hash": "scrypt$x", "roles": "operator"},
    ])
    with pytest.raises(AccountsFileError):
        load_user_records(f)


# ---------- save_user_records: atomic + validated + 0600 ------------------


def test_save_round_trips_and_is_locked_down(tmp_path: Path):
    f = tmp_path / "u.json"
    save_user_records(f, [_rec("op@ut.example")])
    assert [r["email"] for r in load_user_records(f)] == ["op@ut.example"]
    mode = stat.S_IMODE(os.stat(f).st_mode)
    assert mode == 0o600, oct(mode)


def test_save_validates_before_writing(tmp_path: Path):
    f = tmp_path / "u.json"
    save_user_records(f, [_rec("op@ut.example")])
    before = f.read_text(encoding="utf-8")
    with pytest.raises(AccountsFileError):
        save_user_records(f, [{"email": "bad@b.c", "password": "plain"}])
    # the good file is untouched — no partial/temp clobber
    assert f.read_text(encoding="utf-8") == before
    assert not (tmp_path / "u.json.tmp").exists()


# ---------- upsert_user_record --------------------------------------------


def test_upsert_creates(tmp_path: Path):
    f = tmp_path / "u.json"
    out = upsert_user_record(f, email="Op@UT.Example",
                             password_hash=get_password_hash(PW),
                             name="Op", roles=["operator"])
    assert out["created"] is True
    assert out["email"] == "op@ut.example"
    recs = load_user_records(f)
    assert recs[0]["roles"] == ("operator",)


def test_upsert_existing_without_overwrite_raises(tmp_path: Path):
    f = tmp_path / "u.json"
    upsert_user_record(f, email="op@ut.example",
                       password_hash=get_password_hash(PW), roles=["operator"])
    with pytest.raises(AccountsFileError):
        upsert_user_record(f, email="op@ut.example",
                           password_hash=get_password_hash("Other-Pw-1"))


def test_upsert_overwrite_keeps_roles_and_name(tmp_path: Path):
    f = tmp_path / "u.json"
    upsert_user_record(f, email="op@ut.example",
                       password_hash=get_password_hash(PW),
                       name="Op Name", roles=["operator", "staff"])
    new_hash = get_password_hash("Rotated-Pw-2")
    out = upsert_user_record(f, email="op@ut.example",
                             password_hash=new_hash, overwrite=True)
    assert out["created"] is False
    rec = load_user_records(f)[0]
    assert rec["password_hash"] == new_hash       # rotated
    assert rec["roles"] == ("operator", "staff")  # preserved
    assert rec["name"] == "Op Name"               # preserved


# ---------- JsonFileUserStore: lookup + fail-closed -----------------------


def test_store_lookup(tmp_path: Path):
    f = tmp_path / "u.json"
    save_user_records(f, [_rec("op@ut.example")])
    store = JsonFileUserStore(f)
    u = store.get_by_email("OP@ut.example")
    assert u is not None and u.roles == ("operator",)
    assert store.get_by_email("ghost@ut.example") is None
    assert authenticate(store, "op@ut.example", PW) is not None
    assert authenticate(store, "op@ut.example", "wrong") is None


def test_store_missing_file_is_deny_all_then_recovers(tmp_path: Path):
    f = tmp_path / "u.json"
    store = JsonFileUserStore(f)                     # file does not exist yet
    assert len(store) == 0
    assert store.get_by_email("op@ut.example") is None
    save_user_records(f, [_rec("op@ut.example")])   # admin provisions later
    assert store.get_by_email("op@ut.example") is not None


def test_store_hot_reload_on_append(tmp_path: Path):
    f = tmp_path / "u.json"
    save_user_records(f, [_rec("op@ut.example")])
    store = JsonFileUserStore(f)
    assert store.get_by_email("new@ut.example") is None
    # admin runs `axi gate adduser` → appends + bumps mtime; no restart
    upsert_user_record(f, email="new@ut.example",
                       password_hash=get_password_hash(PW), roles=["student"])
    _bump_mtime(f)
    got = store.get_by_email("new@ut.example")
    assert got is not None and got.roles == ("student",)
    assert store.get_by_email("op@ut.example") is not None  # original still there


def test_store_broken_file_is_deny_all_and_recovers(tmp_path: Path):
    f = tmp_path / "u.json"
    save_user_records(f, [_rec("op@ut.example")])
    store = JsonFileUserStore(f)
    assert store.get_by_email("op@ut.example") is not None
    f.write_text("{ this is not json", encoding="utf-8")   # corrupt it
    _bump_mtime(f)
    assert store.get_by_email("op@ut.example") is None      # fail-closed
    save_user_records(f, [_rec("op@ut.example")])           # admin fixes it
    _bump_mtime(f)
    assert store.get_by_email("op@ut.example") is not None  # recovers


def _bump_mtime(path: Path) -> None:
    # Force a distinct mtime so the store's stat-based cache invalidates even
    # when the test writes twice within one filesystem mtime tick.
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 2))
