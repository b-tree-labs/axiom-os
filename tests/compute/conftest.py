# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for tests/compute/.

Generates a temporary node identity and points ``Path.home()`` at the tmp
parent so any test that uses ``axi.compute.dispatch._load_local_identity``
or ``axi.compute.agree.agree`` finds a valid keypair without depending on
the test runner's HOME having one. Without this, fresh-runner CI fails with
``FileNotFoundError: '/home/runner/.axi/identity/identity.json'`` while
local runs (where the developer has an identity) pass — exactly the
"works on my machine" failure mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_axi_identity(tmp_path, monkeypatch):
    """Generate a tmp-path-scoped ~/.axi/identity/ and route Path.home() to it.

    Autouse so every test in this directory gets a clean identity without
    touching the developer's real ~/.axi/. Tests that need to assert against
    a specific identity should override via their own fixture or by using
    ``identity_dir=`` parameters where supported.
    """
    fake_home = tmp_path / "fake-home"
    identity_dir = fake_home / ".axi" / "identity"

    # Generate the identity into the tmp dir before anything else loads it.
    from axiom.vega.federation.identity import generate_identity
    generate_identity(owner="test@example.invalid", keys_dir=identity_dir)

    # Route Path.home() everywhere downstream to the tmp parent.
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    yield fake_home
