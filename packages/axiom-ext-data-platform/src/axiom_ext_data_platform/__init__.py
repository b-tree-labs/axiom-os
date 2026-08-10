# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axiom-ext-data-platform — Bronze/Silver/Gold backends for the Axiom platform.

Backend modules are imported lazily so the base install (without the matching
extra) still imports cleanly. Use the package-level helpers below to pull in
the specific store you want; they raise a clean ImportError if the extra is
missing.

    from axiom_ext_data_platform import DuckDBBronzeReceiptStore  # needs [duckdb]
    from axiom_ext_data_platform import MemoryBronzeReceiptStore  # always works
"""

from __future__ import annotations

from axiom_ext_data_platform.memory_store import MemoryBronzeReceiptStore

__all__ = [
    "MemoryBronzeReceiptStore",
    "DuckDBBronzeReceiptStore",
]


def __getattr__(name: str):
    # Lazy resolution so `import axiom_ext_data_platform` doesn't require
    # every backend's deps to be installed.
    if name == "DuckDBBronzeReceiptStore":
        from axiom_ext_data_platform.duckdb_store import DuckDBBronzeReceiptStore
        return DuckDBBronzeReceiptStore
    raise AttributeError(f"module 'axiom_ext_data_platform' has no attribute {name!r}")
