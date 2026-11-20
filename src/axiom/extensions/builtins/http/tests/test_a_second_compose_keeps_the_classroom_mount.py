# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The second `axi serve` on a home still serves `/classroom`.

Found walking the colleague quickstart on 2026-10-21 in a clean home. The first
`neut serve --list` printed five routes. The second printed four, preceded by a
traceback::

    built-in mount factory <function classroom_mount_spec ...> failed
    IdentityDamaged: .../classroom/keys/private.pem already holds this node's
    private key, so a new identity was NOT generated

`classroom_mount_spec` called `generate_identity` every time it composed. The
guard in `generate_identity` (never overwrite the only copy of a signing key) is
right; the caller was wrong to ask for a new identity when one existed. The
coordinator's identity is made once and kept, so the mount must load it.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.http.mounts import classroom_mount_spec


@pytest.fixture(autouse=True)
def a_clean_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)


def test_the_first_compose_mounts_classroom():
    assert classroom_mount_spec().prefix == "/classroom"


def test_the_second_compose_mounts_classroom_too():
    classroom_mount_spec()
    assert classroom_mount_spec().prefix == "/classroom"


def test_the_coordinator_identity_is_the_same_one_each_time(tmp_path):
    """Kept, not regenerated: the key is the only copy signatures verify against."""
    keys = tmp_path / "state" / "classroom" / "keys"
    classroom_mount_spec()
    first = (keys / "private.pem").read_bytes()
    classroom_mount_spec()
    assert (keys / "private.pem").read_bytes() == first


def test_a_damaged_identity_still_fails_loudly(tmp_path):
    """Loading must not turn damage into a silent fresh start."""
    from axiom.vega.federation.identity import IdentityDamaged

    classroom_mount_spec()
    (tmp_path / "state" / "classroom" / "keys" / "identity.json").unlink()
    with pytest.raises(IdentityDamaged):
        classroom_mount_spec()
