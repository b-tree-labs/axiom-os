# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Round-trip + parsing tests for ``SecretRef``."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.secrets import SecretRef


@pytest.mark.parametrize(
    "url,scheme,path,version",
    [
        ("openbao://kv/data/example-host/dp1/db/password", "openbao",
         "kv/data/example-host/dp1/db/password", None),
        ("env://NEUT_PG_PASSWORD", "env", "NEUT_PG_PASSWORD", None),
        ("openbao://kv/data/foo?version=3", "openbao", "kv/data/foo", 3),
        ("kubernetes://secret/axiom-data/dp1-db", "kubernetes",
         "secret/axiom-data/dp1-db", None),
    ],
)
def test_parse_roundtrip(url, scheme, path, version):
    ref = SecretRef.parse(url)
    assert ref.scheme == scheme
    assert ref.path == path
    assert ref.version == version


def test_str_roundtrip_preserves_version():
    original = "openbao://kv/data/foo?version=3"
    assert str(SecretRef.parse(original)) == original


def test_str_roundtrip_no_version():
    original = "openbao://kv/data/example-host/dp1/db/password"
    assert str(SecretRef.parse(original)) == original


def test_missing_scheme_rejected():
    with pytest.raises(ValueError, match="missing scheme"):
        SecretRef.parse("kv/data/foo")


def test_missing_path_rejected():
    with pytest.raises(ValueError, match="missing path"):
        SecretRef.parse("openbao://")


def test_query_params_preserved():
    ref = SecretRef.parse("openbao://kv/data/foo?namespace=tenant-a")
    assert ref.query == {"namespace": "tenant-a"}
