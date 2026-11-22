# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for the runs-and-workspaces acceptance tests (ADR-250).

`attest_db` binds `real_postgres` first, the root conftest's throwaway database, so a signing
grant is minted and spent against a real attest schema and never a developer's own.
"""

import pytest

from axiom.extensions.builtins.attest.tests.conftest import (
    attest_db as _attest_schema,  # noqa: F401
)
from axiom.extensions.builtins.attest.tests.conftest import node_signer  # noqa: F401


@pytest.fixture
def attest_db(real_postgres, _attest_schema):  # noqa: F811
    """A private attest schema at migration head, in a database made for this run."""
    return _attest_schema
