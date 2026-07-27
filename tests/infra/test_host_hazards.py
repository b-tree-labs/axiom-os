# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Host-hazard registry read seam (ADR-081).

Pins the reliability properties of the projection: deterministic keyed lookup,
host-scoping, supersession (mitigated != active), and fail-safe behavior when
no provider is present or the provider raises.
"""

from __future__ import annotations

from axiom.infra.host_hazards import (
    SIG_APT_CATCH_ALL_PIN,
    HostHazard,
    NullHostHazardProvider,
    StaticHostHazardProvider,
    default_host_hazard_provider,
    lookup_active_hazard,
)


def _hz(host="host-a", status="active"):
    return HostHazard(
        host_id=host,
        signature=SIG_APT_CATCH_ALL_PIN,
        category="environment",
        consequence="fix-broken removes the running GPU driver",
        remediation="scope the pin; run 'apt-get -s -f install' first",
        status=status,
        recorded_at="2026-07-06",
    )


def test_hazard_is_active_flag():
    assert _hz(status="active").is_active is True
    assert _hz(status="mitigated").is_active is False


def test_static_provider_returns_active_hazard_for_its_host():
    prov = StaticHostHazardProvider("host-a", {SIG_APT_CATCH_ALL_PIN: _hz()})
    hz = prov.active(SIG_APT_CATCH_ALL_PIN)
    assert hz is not None
    assert hz.consequence.startswith("fix-broken")


def test_static_provider_scopes_to_host():
    # A hazard recorded for host-a must not surface on host-b's provider.
    prov = StaticHostHazardProvider("host-b", {SIG_APT_CATCH_ALL_PIN: _hz(host="host-a")})
    assert prov.active(SIG_APT_CATCH_ALL_PIN) is None


def test_static_provider_hides_mitigated():
    prov = StaticHostHazardProvider(
        "host-a", {SIG_APT_CATCH_ALL_PIN: _hz(status="mitigated")}
    )
    assert prov.active(SIG_APT_CATCH_ALL_PIN) is None


def test_static_provider_unknown_signature():
    prov = StaticHostHazardProvider("host-a", {SIG_APT_CATCH_ALL_PIN: _hz()})
    assert prov.active("no-such-signature") is None


def test_null_provider_never_returns():
    assert NullHostHazardProvider().active(SIG_APT_CATCH_ALL_PIN) is None


def test_default_provider_is_safe():
    assert default_host_hazard_provider().active(SIG_APT_CATCH_ALL_PIN) is None


# ---- lookup_active_hazard fail-safe contract --------------------------------


def test_lookup_no_provider_is_unavailable_not_healthy():
    hz, available = lookup_active_hazard(None, SIG_APT_CATCH_ALL_PIN)
    assert hz is None
    assert available is False  # NOT (None, True) — absence must be visible


def test_lookup_hit():
    prov = StaticHostHazardProvider("host-a", {SIG_APT_CATCH_ALL_PIN: _hz()})
    hz, available = lookup_active_hazard(prov, SIG_APT_CATCH_ALL_PIN)
    assert hz is not None and available is True


def test_lookup_miss_is_available():
    prov = StaticHostHazardProvider("host-a", {})
    hz, available = lookup_active_hazard(prov, SIG_APT_CATCH_ALL_PIN)
    assert hz is None and available is True


def test_lookup_provider_raises_is_failsafe():
    class _Boom:
        host_id = "host-a"

        def active(self, signature):
            raise RuntimeError("registry unreachable")

    hz, available = lookup_active_hazard(_Boom(), SIG_APT_CATCH_ALL_PIN)
    assert hz is None
    assert available is False  # a raising registry is unavailable, never healthy
