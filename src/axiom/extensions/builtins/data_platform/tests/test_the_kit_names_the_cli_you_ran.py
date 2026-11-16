# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The data kit's next steps name the command the person actually runs (#1163).

Under a product's own CLI name, ``kit-init`` and ``kit-up`` told people to run
``axi data kit-up``, a command their install may not even have on PATH.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.data_platform.skills import kit as kit_skills
from axiom.infra import branding
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture
def product_cli():
    branding.register(branding.BrandingConfig(cli_name="acme"))
    yield "acme"
    branding.reset()


def test_kit_init_next_step_uses_the_product_cli(tmp_path, product_cli):
    ctx = SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"))
    res = kit_skills.kit_init({"dir": str(tmp_path), "tenant": "site-a"}, ctx)
    assert res.ok, res.errors
    assert f"`{product_cli} data kit-up`" in res.value["text"]
    assert "`axi data" not in res.value["text"]
