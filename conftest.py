# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

# Root conftest — makes shared fixtures available to ALL test directories,
# including colocated extension tests in src/axiom/extensions/builtins/.
#
# Fixtures are defined in tests/conftest.py and re-exported here so that
# pytest discovers them regardless of which testpath a test lives under.

from tests.conftest import *  # noqa: F401,F403
