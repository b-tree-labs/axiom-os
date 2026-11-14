# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Thin CLI shim — defers to ``axiom.compute_decomposition.cli.compute``."""

from __future__ import annotations

from axiom.compute_decomposition.cli.compute import main as _impl_main


def main(argv: list[str] | None = None) -> int:
    return _impl_main(argv)


__all__ = ["main"]
