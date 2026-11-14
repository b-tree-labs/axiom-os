# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Test config for the publishing extension."""

import pytest


@pytest.fixture(autouse=True)
def _publisher_state_stays_out_of_the_checkout(tmp_path, monkeypatch):
    """A test's publisher state lands in its own tmp dir, never in the checkout.

    ``PublisherConfig.repo_root`` defaults to the project root discovered at import,
    which under pytest is the checkout, and the engine creates
    ``<root>/.<cli>/publisher/`` on construction. Every test that built an engine
    with the default config left that directory in the worktree root, where the
    repo hygiene check then failed someone else's push.
    """
    from axiom.extensions.builtins.publishing import config

    root = tmp_path / "publisher-project"
    root.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
