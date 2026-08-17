# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Belt-and-suspenders check — every built-in provider must end up in
the registry's ``available_kinds()``. SEC-3 shipped a bug where the
kubernetes provider was importable but not registered; this test
prevents that class of regression."""

from __future__ import annotations

from axiom.extensions.builtins.secrets import SecretStoreRegistry


_BUILTIN_KINDS = ("env", "openbao", "kubernetes", "gcp")


def test_every_builtin_kind_is_registered():
    kinds = set(SecretStoreRegistry.available_kinds())
    for k in _BUILTIN_KINDS:
        assert k in kinds, (
            f"built-in provider kind {k!r} is missing from the registry; "
            f"check providers/_register.py"
        )
