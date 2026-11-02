# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Harness adapters for the portable overhead benchmark.

An adapter exposes ``build() -> Harness``. Adapters for OTHER frameworks belong
wherever that framework is installed and are loaded by dotted path, so this
package never imports — or depends on — a competitor's runtime.
"""
