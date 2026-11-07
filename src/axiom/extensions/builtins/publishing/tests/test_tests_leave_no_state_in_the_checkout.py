# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Building the engine with the default config under test writes nowhere in the checkout."""

from pathlib import Path

from axiom.extensions.builtins.publishing import config
from axiom.extensions.builtins.publishing.engine import PublisherEngine
from axiom.infra.paths import project_dir_name

CHECKOUT = Path(__file__).resolve().parents[6]


def test_default_engine_state_goes_to_the_tests_tmp_root():
    before = (CHECKOUT / project_dir_name()).exists()
    engine = PublisherEngine()
    assert str(engine.config.repo_root) == str(config.PROJECT_ROOT)
    assert "publisher-project" in str(config.PROJECT_ROOT)
    assert (CHECKOUT / project_dir_name()).exists() == before
