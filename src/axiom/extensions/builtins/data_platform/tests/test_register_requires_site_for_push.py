# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A connector that can ACCEPT rows must be able to LAND them.

Found live: eleven push connectors registered without a site accepted
rows with 2xx forever while conform silently skipped every one of them
(``unmapped_connectors``, ``rows_in: 0``) — discovery deferred months,
to conform time. Registration is the moment the operator is present, so
a push connector without ``--site`` is refused there, with an explicit
``--allow-unmapped`` escape hatch for the deliberate case. Pull kinds
keep the existing loud warning (their rows arrive by our own hand, not
a partner's).
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.data_platform.skills import register
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture()
def ctx(tmp_path):
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=tmp_path,
        logger=logging.getLogger("test-register"),
    )


def _push_params(tmp_path, **extra):
    return {
        "name": "test-loop",
        "kind": "push",
        "bronze_root": str(tmp_path / "bronze"),
        **extra,
    }


def test_push_without_site_is_refused(ctx, tmp_path):
    result = register.run(_push_params(tmp_path), ctx)
    assert result.ok is False
    joined = " ".join(result.errors)
    assert "--site" in joined
    assert "--allow-unmapped" in joined
    assert "conform" in joined.lower()


def test_push_without_site_writes_nothing(ctx, tmp_path):
    register.run(_push_params(tmp_path), ctx)
    connector_file = tmp_path / "plinth" / "connectors" / "test-loop.toml"
    assert not connector_file.exists()


def test_push_with_allow_unmapped_registers_with_loud_warning(ctx, tmp_path):
    result = register.run(_push_params(tmp_path, allow_unmapped=True), ctx)
    assert result.ok is True, result.errors
    assert any("WITHOUT a site" in a for a in result.actions_taken)


def test_push_with_site_registers_without_warning(ctx, tmp_path):
    result = register.run(_push_params(tmp_path, site="site-a"), ctx)
    assert result.ok is True, result.errors
    assert not any("WITHOUT a site" in a for a in result.actions_taken)
    assert result.value["site"] == "site-a"


def test_pull_kind_without_site_still_warns_not_refuses(ctx, tmp_path):
    result = register.run(
        {
            "name": "test-pull",
            "kind": "http-tabular",
            "bronze_root": str(tmp_path / "bronze"),
            "kind_params": {"url": "https://example.test/data.csv", "schema_ref": "s1"},
        },
        ctx,
    )
    assert result.ok is True, result.errors
    assert any("WITHOUT a site" in a for a in result.actions_taken)
