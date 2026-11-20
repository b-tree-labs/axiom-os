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


def test_building_an_engine_creates_no_state_dir_until_it_writes(tmp_path):
    """Any caller, not only these tests, can build an engine for a root without
    leaving an empty ``publisher/`` behind (a chat tool did, in the checkout, #1169)."""
    from axiom.extensions.builtins.publishing.config import PublisherConfig
    from axiom.extensions.builtins.publishing.registry import LinkEntry

    engine = PublisherEngine(PublisherConfig(repo_root=tmp_path))
    engine.list_providers()
    assert not (tmp_path / project_dir_name()).exists()

    engine.registry.update(LinkEntry(doc_id="a", source_path="docs/a.md", published_url="file:///a"))
    assert (tmp_path / project_dir_name() / "publisher" / "publisher-registry.json").is_file()
