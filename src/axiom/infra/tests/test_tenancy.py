# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Tenant offboarding (ADR-025 §9).

The danger here is not a delete that fails loudly. It is a delete that
succeeds while quietly missing a subsystem nobody remembered holds tenant
data — so most of these tests are about what the report *admits*.
"""

from __future__ import annotations

import pytest

from axiom.infra.tenancy import (
    UNREAPED,
    ReaperRegistry,
    TenantResource,
    offboard,
)


class _Reaper:
    def __init__(self, subsystem="fake", items=(), fail_find=False, fail_reap=False):
        self.subsystem = subsystem
        self._items = list(items)
        self._fail_find = fail_find
        self._fail_reap = fail_reap
        self.reaped = False

    def find(self, site):
        if self._fail_find:
            raise RuntimeError("cannot enumerate")
        return [
            TenantResource(subsystem=self.subsystem, kind="thing", identifier=f"{site}-{i}")
            for i in self._items
        ]

    def reap(self, site):
        if self._fail_reap:
            raise RuntimeError("cannot delete")
        self.reaped = True
        return [f"{self.subsystem}: removed {len(self._items)} for {site}"]


def _registry(*reapers) -> ReaperRegistry:
    r = ReaperRegistry()
    for reaper in reapers:
        r.register(reaper)
    return r


# ------------------------------------------------------------------ honesty


def test_an_offboard_is_not_complete_while_subsystems_are_unreaped() -> None:
    """The whole point: exiting zero is not the same as having deleted a
    tenant, and the report must not imply otherwise."""
    report = offboard("acu", dry_run=False, registry=_registry(_Reaper()))
    assert report.complete is False
    assert report.unreaped  # whatever is still outstanding, it is named


def test_complete_only_when_nothing_is_outstanding() -> None:
    report = offboard("acu", dry_run=False, registry=_registry(_Reaper()), unreaped=())
    assert report.complete is True


def test_the_unreaped_list_names_the_surfaces_that_still_hold_data() -> None:
    """This list is the ADR-025 implementation checklist, printed every time
    someone removes a tenant so it cannot rot in a document.

    Asserted as a shrinking property rather than a fixed membership: entries
    leave as reapers land (``rag`` already has), and a test that had to be
    edited for each one would teach people to edit it without thinking."""
    assert set(UNREAPED) <= {
        "rag",
        "caches",
        "sessions",
        "memory",
        "evals",
        "traces",
        "bronze",
        "silver",
    }
    assert UNREAPED, "an empty list would mean every surface is reaped — prove it first"


# ------------------------------------------------------------------ safety


def test_dry_run_is_the_default_and_deletes_nothing() -> None:
    reaper = _Reaper(items=[1, 2])
    report = offboard("acu", registry=_registry(reaper))
    assert report.dry_run is True
    assert len(report.found) == 2
    assert report.removed == []
    assert reaper.reaped is False


def test_an_unnamed_tenant_is_refused() -> None:
    for site in ("", "   "):
        with pytest.raises(ValueError, match="refusing to offboard an unnamed tenant"):
            offboard(site)


def test_a_reaper_must_name_its_subsystem() -> None:
    class Nameless:
        subsystem = ""

    with pytest.raises(ValueError, match="must name its subsystem"):
        ReaperRegistry().register(Nameless())


# ----------------------------------------------------------------- failures


def test_one_subsystem_failing_to_enumerate_does_not_hide_the_others() -> None:
    good = _Reaper(subsystem="good", items=[1])
    report = offboard(
        "acu", dry_run=False, registry=_registry(_Reaper(subsystem="bad", fail_find=True), good)
    )
    assert good.reaped is True
    assert any("bad: find failed" in f for f in report.failed)
    assert report.complete is False


def test_a_failed_deletion_is_reported_not_swallowed() -> None:
    report = offboard("acu", dry_run=False, registry=_registry(_Reaper(items=[1], fail_reap=True)))
    assert any("reap failed" in f for f in report.failed)
    assert report.complete is False


def test_nothing_found_means_nothing_reaped() -> None:
    reaper = _Reaper(items=[])
    offboard("acu", dry_run=False, registry=_registry(reaper))
    assert reaper.reaped is False


def test_report_serializes_for_a_receipt() -> None:
    import json

    report = offboard("acu", registry=_registry(_Reaper(items=[1])))
    json.dumps(report.as_dict())
