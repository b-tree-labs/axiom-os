# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Moved under the storage provider (ADR-220 phase 1).

This import path IS the moved module (one object in ``sys.modules``), so a caller
or a test that patches an attribute here patches the real one. Removed in phase 4.
"""

import sys

from axiom.extensions.builtins.connector.storage.box import oauth_auth as _moved

sys.modules[__name__] = _moved
