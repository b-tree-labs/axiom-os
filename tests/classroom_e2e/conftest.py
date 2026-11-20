# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom e2e test fixtures.

Opt-in via `@pytest.mark.classroom_e2e`. Requires Docker for
multi-node federation scenarios; pure-Python scenarios run without.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if "classroom_e2e" in str(item.fspath):
            item.add_marker(pytest.mark.classroom_e2e)
