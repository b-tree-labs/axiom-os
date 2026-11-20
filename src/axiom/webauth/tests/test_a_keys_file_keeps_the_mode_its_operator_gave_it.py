# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Issuing or revoking one key must not lock every other key out.

An ingest edge runs in a container as a user that is not the operator, and
reads the keys file the operator owns through a read-only mount. The file holds
hashes only. Every write used to put it back to 0600, so the first
``gate issue`` or ``gate revoke`` after the operator made it readable turned it
unreadable to the edge: the store failed closed and refused every key for every
site, not just the one being changed. Fixing the mode by hand did not even help
until something touched the file again, because the store reloaded on a changed
modification time and a ``chmod`` changes only the status-change time.

What has to hold:

- a file the operator made readable stays readable through issue and revoke;
- a file this code creates is private (0600), as before;
- a change the store can see is a change it reloads, including one that leaves
  the modification time as it was;
- when the file cannot be read, the log says why, once.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from axiom.webauth.api_keys import (
    JsonFileApiKeyStore,
    append_api_key_record,
    mint_api_key,
    revoke_api_key_record,
)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _issue(path: Path, principal="@svc:org"):
    token, record = mint_api_key(principal=principal, scopes=("data_platform:invoke",))
    append_api_key_record(path, record)
    return token, record


def test_a_file_this_code_creates_is_private(tmp_path):
    f = tmp_path / "keys.json"
    _issue(f)
    assert _mode(f) == 0o600


def test_an_operator_readable_file_stays_readable_through_issue_and_revoke(tmp_path):
    f = tmp_path / "keys.json"
    _, first = _issue(f)
    os.chmod(f, 0o644)
    _issue(f, principal="@other:org")
    assert _mode(f) == 0o644
    revoke_api_key_record(f, first["key_id"])
    assert _mode(f) == 0o644


def test_a_tighter_mode_the_operator_chose_is_kept_too(tmp_path):
    f = tmp_path / "keys.json"
    _issue(f)
    os.chmod(f, 0o640)
    _issue(f, principal="@other:org")
    assert _mode(f) == 0o640


def test_a_rewrite_that_keeps_the_modification_time_is_still_seen(tmp_path):
    f = tmp_path / "keys.json"
    token, record = _issue(f)
    store = JsonFileApiKeyStore(f)
    assert store.resolve(token) is not None
    before = f.stat()
    revoke_api_key_record(f, record["key_id"])
    os.utime(f, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert store.resolve(token) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_fixing_the_mode_alone_brings_the_keys_back(tmp_path):
    f = tmp_path / "keys.json"
    token, _ = _issue(f)
    store = JsonFileApiKeyStore(f)
    assert store.resolve(token) is not None
    os.chmod(f, 0o000)
    assert store.resolve(token) is None  # can't read: deny-all, not the stale copy
    os.chmod(f, 0o600)  # the only thing the operator did
    assert store.resolve(token) is not None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_file_is_said_once_and_says_why(tmp_path, caplog):
    f = tmp_path / "keys.json"
    token, _ = _issue(f)
    store = JsonFileApiKeyStore(f)
    os.chmod(f, 0o000)
    with caplog.at_level(logging.WARNING, logger="axiom.webauth.api_keys"):
        for _ in range(5):
            assert store.resolve(token) is None
    said = [r for r in caplog.records if "keys file" in r.getMessage()]
    assert len(said) == 1
    assert "Permission" in said[0].getMessage() or "permission" in said[0].getMessage()
    assert token not in caplog.text
