# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The attest fixtures, shared with the logbook acceptance tests.

The extension's `attest_db` skips when `AXIOM_DB_URL` is unset, and the Unit Tests job in CI
does not set it, so there it skips every time. A skipped test cannot fail and cannot prove
anything. Here `attest_db` first binds `real_postgres`, the root conftest's throwaway
database, so these tests run in CI, and a missing server is a failure there, not a skip.
"""

import pytest

from axiom.extensions.builtins.attest.tests.conftest import attest_db as _attest_schema  # noqa: F401
from axiom.extensions.builtins.attest.tests.conftest import node_signer  # noqa: F401
from axiom.extensions.builtins.attest.tests.test_api import client  # noqa: F401
from axiom.extensions.builtins.attest.tests.test_skills import bound_signer, roles  # noqa: F401


@pytest.fixture
def attest_db(real_postgres, _attest_schema):  # noqa: F811
    """A private attest schema at migration head, in a database made for this run."""
    return _attest_schema
