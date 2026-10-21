# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``Secret`` value wrapper."""

from __future__ import annotations

from axiom.extensions.builtins.secrets import Secret


def test_as_str_decodes_value():
    s = Secret(value=b"hunter2")
    assert s.as_str() == "hunter2"


def test_context_manager_zeroes_value_on_exit():
    s = Secret(value=b"hunter2")
    with s as scoped:
        assert scoped.value == b"hunter2"
    assert s.value == b"\x00" * 7  # zeroed best-effort


def test_lease_and_version_default_none():
    s = Secret(value=b"x")
    assert s.lease_id is None
    assert s.version is None


def test_metadata_carries_provenance():
    s = Secret(value=b"x", metadata={"backend": "openbao", "mount": "kv"})
    assert s.metadata["backend"] == "openbao"
