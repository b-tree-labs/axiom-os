# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Receipts surface — the appkit-shell console every Receipts-program feature renders into."""

__all__ = ["mount_spec"]


def mount_spec():
    from axiom.extensions.builtins.receipts.mount import mount_spec as _spec

    return _spec()
