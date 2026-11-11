# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A conform pass that wrote refreshes the serving catalog, in the watcher's own process.

Measured on a node, 2026-11-10: the serving catalog's newest readings were two
months old while gold held today's. The webapp extension's skills module had
``bind(registry)`` but no ``bind_default()``, the skill loader binds by
``bind_default``, so ``webapp.project`` was never registered in the conform
watcher, and the hook that refreshes the catalog after each pass skipped in
silence on every pass.
"""

from __future__ import annotations

from types import SimpleNamespace

from axiom.extensions.builtins.data_platform.skills import conform_run
from axiom.infra.skills import SkillRegistry, SkillResult


def test_the_watcher_can_always_bind_the_projection():
    registry = conform_run._bind_projection()
    assert registry is not None and registry.has(conform_run.CATALOG_PROJECTION)


def test_webapp_binds_into_the_default_registry_by_the_loaders_name():
    from axiom.extensions.builtins.webapp import skills

    assert skills.bind_default().has("webapp.project")


def test_a_pass_that_wrote_projects_even_when_the_process_never_bound_it(monkeypatch):
    calls = []
    bound = SkillRegistry()
    bound.register(conform_run.CATALOG_PROJECTION,
                   lambda params, ctx=None: calls.append(params) or SkillResult(ok=True))
    monkeypatch.setattr(conform_run, "default_registry", lambda: SkillRegistry(), raising=False)
    monkeypatch.setattr("axiom.infra.skills.default_registry", lambda: SkillRegistry())
    monkeypatch.setattr(conform_run, "_bind_projection", lambda: bound)
    result = SkillResult(ok=True, value={})
    conform_run._project_catalog({"rows_out": 3}, result, SimpleNamespace(registry=None))
    assert calls == [{}]
    assert result.value["catalog_projected"] is True


def test_an_install_without_a_catalog_says_so_once(monkeypatch, caplog):
    monkeypatch.setattr("axiom.infra.skills.default_registry", lambda: SkillRegistry())
    monkeypatch.setattr(conform_run, "_bind_projection", lambda: None)
    monkeypatch.setattr(conform_run, "_ABSENT_SAID", False)
    for _ in range(2):
        result = SkillResult(ok=True, value={})
        conform_run._project_catalog({"rows_out": 1}, result, SimpleNamespace(registry=None))
        assert result.value["catalog_projected"] is None
    assert sum("is not refreshed" in r.getMessage() for r in caplog.records) == 1
