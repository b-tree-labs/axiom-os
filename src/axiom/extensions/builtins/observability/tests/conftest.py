# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for observability extension tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture
def skill_ctx(tmp_path: Path) -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=tmp_path,
        logger=logging.getLogger("observability.tests"),
        user_prompt=None,
    )
