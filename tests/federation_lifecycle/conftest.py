# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pytest plumbing for federation lifecycle tests.

* Skip the whole suite cleanly when Docker isn't available — these tests
  must never fail for infrastructure-absence reasons on dev laptops or CI
  runners that don't opt in.
* Provide a ``harness`` fixture so individual scenarios don't have to
  repeat the context-manager dance.
"""

from __future__ import annotations

import pytest

from tests.federation_lifecycle.harness import FederationHarness, docker_available


def pytest_collection_modifyitems(config, items):
    # Every test in this package implicitly has the marker — saves authors
    # from having to remember it on each new scenario.
    for item in items:
        if "federation_lifecycle" in str(item.fspath):
            item.add_marker(pytest.mark.federation_lifecycle)


@pytest.fixture(scope="function")
def harness(request):
    ok, reason = docker_available()
    if not ok:
        pytest.skip(f"federation_lifecycle: {reason}")
    project = f"axifed_{request.node.name}".replace("[", "_").replace("]", "_").lower()
    fed = FederationHarness(project=project)
    try:
        yield fed
    finally:
        fed.teardown()
