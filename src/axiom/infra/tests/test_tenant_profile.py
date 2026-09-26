# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""The tenant record — where corpus subscriptions live."""

from __future__ import annotations

from axiom.infra.tenancy import (
    ReaperRegistry,
    TenantProfile,
    TenantProfileReaper,
    delete_tenant,
    list_tenants,
    load_tenant,
    offboard,
    save_tenant,
    subscriptions_for,
)

ACU = TenantProfile(
    site="acu-flowloop",
    display_name="ACU Flow Loop",
    subscriptions=("msr-literature", "reactor-general"),
)


def test_a_profile_round_trips(tmp_path) -> None:
    save_tenant(ACU, state_dir=tmp_path)
    got = load_tenant("acu-flowloop", state_dir=tmp_path)
    assert got == ACU


def test_the_record_is_reviewable_as_a_diff(tmp_path) -> None:
    """Data, not code — so approving a tenant's library is reading a file."""
    text = (save_tenant(ACU, state_dir=tmp_path)).read_text(encoding="utf-8")
    assert '"msr-literature"' in text
    assert "never deletes these" in text


def test_a_tenant_with_no_record_subscribes_to_nothing(tmp_path) -> None:
    """Under-serving rather than over-sharing: the safe direction to fail."""
    assert subscriptions_for("nobody", state_dir=tmp_path) == []


def test_a_malformed_record_reads_as_absent_not_as_permissive(tmp_path) -> None:
    d = tmp_path / "tenants"
    d.mkdir()
    (d / "broken.toml").write_text("[tenant\n", encoding="utf-8")
    (d / "empty.toml").write_text("[tenant]\n", encoding="utf-8")
    assert load_tenant("broken", state_dir=tmp_path) is None
    assert load_tenant("empty", state_dir=tmp_path) is None
    assert subscriptions_for("broken", state_dir=tmp_path) == []


def test_listing_skips_unreadable_records(tmp_path) -> None:
    save_tenant(ACU, state_dir=tmp_path)
    (tmp_path / "tenants" / "junk.toml").write_text("nonsense", encoding="utf-8")
    assert [t.site for t in list_tenants(state_dir=tmp_path)] == ["acu-flowloop"]


def test_delete_is_idempotent(tmp_path) -> None:
    save_tenant(ACU, state_dir=tmp_path)
    assert delete_tenant("acu-flowloop", state_dir=tmp_path) is True
    assert delete_tenant("acu-flowloop", state_dir=tmp_path) is False


# --------------------------------------------------------------- offboarding


def test_offboard_removes_the_record(tmp_path) -> None:
    save_tenant(ACU, state_dir=tmp_path)
    registry = ReaperRegistry()
    registry.register(TenantProfileReaper(state_dir=tmp_path))
    report = offboard("acu-flowloop", dry_run=False, registry=registry)
    assert report.removed
    assert load_tenant("acu-flowloop", state_dir=tmp_path) is None


def test_offboard_says_the_subscriptions_are_not_deleted(tmp_path) -> None:
    """The distinction stated where an operator will read it: removing a
    reader does not remove the library."""
    save_tenant(ACU, state_dir=tmp_path)
    found = list(TenantProfileReaper(state_dir=tmp_path).find("acu-flowloop"))
    assert "2 subscription(s), which are not deleted" in found[0].detail


def test_offboarding_one_tenant_leaves_the_others(tmp_path) -> None:
    save_tenant(ACU, state_dir=tmp_path)
    save_tenant(TenantProfile(site="vcu-flowloop"), state_dir=tmp_path)
    registry = ReaperRegistry()
    registry.register(TenantProfileReaper(state_dir=tmp_path))
    offboard("acu-flowloop", dry_run=False, registry=registry)
    assert [t.site for t in list_tenants(state_dir=tmp_path)] == ["vcu-flowloop"]
