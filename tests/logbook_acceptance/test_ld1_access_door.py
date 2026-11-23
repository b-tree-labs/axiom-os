# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.access`` is a door (#2179, LD-1-A5): the ids and counts it returns obey the same
deny and hidden-site rules as the records they name. The generated matrix in test_ld1.py covers
its deny reasons; this file covers what a *permitted* caller may see in the answer."""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import registry  # noqa: E402
from axiom.governance import doors as g  # noqa: E402
from axiom.infra import audit_trail  # noqa: E402
from axiom.infra.skill_dispatch import invoke_capability  # noqa: E402
from tests.logbook_acceptance.test_ld1 import (  # noqa: E402,F401
    LOG,
    ME,
    OTHER,
    SITE,
    _ctx,
    restore_log,
    world,
)


def _seed(state, *, logbook, site, ident):
    audit_trail.record_invocation(
        state_dir=state,
        skill="attest.read.records",
        params={"logbook": logbook},
        principal=ME,
        site=site,
        outcome="ok",
        errors_count=0,
        access={
            "what": "records",
            "logbook": logbook,
            "site": site,
            "ids": [ident],
            "count": 1,
            "surface": "api",
            "person": ME,
        },
    )


def test_the_access_skill_is_a_registered_door():
    door = g.get("attest.access")
    assert door.skill == "attest.access" and set(door.transports) == set(g.SKILL_TRANSPORTS)


@pytest.mark.acceptance("LD-1-A5")
def test_access_lists_only_reads_of_logs_and_sites_the_caller_may_read(world, restore_log):  # noqa: F811
    state = world["state"]
    book = registry.get(LOG)
    registry.register(replace(book, id="closed_log", handle="closed", readers=("auditor",)))
    try:
        _seed(state, logbook=LOG, site=SITE, ident="id-visible")
        _seed(state, logbook="closed_log", site=SITE, ident="id-closed-log")
        _seed(state, logbook=LOG, site="site-b", ident="id-hidden-site")
        c = _ctx(state, OTHER, "cli")
        for params in ({"site": SITE}, {"site": SITE, "logbook": LOG}):
            result = invoke_capability(c.registry, "attest.access", params, c, surface="cli")
            assert result.ok, result
            ids = {i for row in result.value["reads"] for i in row["ids"]}
            assert "id-visible" in ids
            assert not ids & {"id-closed-log", "id-hidden-site"}, ids
    finally:
        registry._logbooks.pop("closed_log", None)  # noqa: SLF001
