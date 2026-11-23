# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Effect-checked status bindings (ADR-119 D4, spec §4).

Structure rule: every kind has (a) a GREEN case citing observed-effect
evidence and (b) at least one negative proving the GREEN predicate can
fail — a green pill that cannot fail is the defect class this extension
exists to catch, so its own tests must not exhibit it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.fleet.status import (
    Status,
    evaluate_report,
    rollup,
)

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
FRESH = NOW - timedelta(minutes=5)
CADENCE_15M = 15 * 60
CADENCE_DAILY = 24 * 3600


def _eval(kind, payload, received_at=FRESH, cadence=CADENCE_15M):
    return evaluate_report(
        kind=kind,
        payload=payload,
        received_at=received_at,
        cadence_seconds=cadence,
        now=NOW,
    )


# ---------------------------------------------------------------- staleness


def test_staleness_beats_content_for_every_kind():
    """A report older than 3x cadence is STALE regardless of how green
    its content claims to be — no last-known-green."""
    old = NOW - timedelta(seconds=CADENCE_15M * 3 + 1)
    for kind, payload in [
        ("heartbeat", {}),
        ("service_health", {"services": [{"name": "db", "status": "healthy", "latency_ms": 3}]}),
        ("backup", {"artifact": "/b/x.dump", "size_bytes": 10, "created_at": old.isoformat()}),
    ]:
        ev = _eval(kind, payload, received_at=old)
        assert ev.status is Status.STALE, kind
        # Plain wording (2026-09-24): the evidence is read by a person, so
        # it says when and how often rather than "past 3x cadence (900s)".
        assert "The last report arrived" in ev.evidence, kind
        assert "This node reports every" in ev.evidence, kind


def test_exactly_at_threshold_is_not_stale():
    at_limit = NOW - timedelta(seconds=CADENCE_15M * 3)
    assert _eval("heartbeat", {}, received_at=at_limit).status is Status.GREEN


# ---------------------------------------------------------------- heartbeat


def test_heartbeat_fresh_is_green_freshness_is_the_effect():
    ev = _eval("heartbeat", {})
    assert ev.status is Status.GREEN
    assert ev.evidence == "Reported on time."


def test_heartbeat_green_can_fail():
    ev = _eval("heartbeat", {}, received_at=NOW - timedelta(hours=2))
    assert ev.status is Status.STALE


# ----------------------------------------------------------- service_health


def _svc(status, latency=3.0, name="db"):
    d = {"name": name, "status": status}
    if latency is not None:
        d["latency_ms"] = latency
    return d


def test_service_health_all_healthy_with_latency_is_green():
    ev = _eval("service_health", {"services": [_svc("healthy"), _svc("healthy", 8.1, "api")]})
    assert ev.status is Status.GREEN


def test_service_health_any_unhealthy_is_failed():
    ev = _eval("service_health", {"services": [_svc("healthy"), _svc("unhealthy", name="api")]})
    assert ev.status is Status.FAILED
    assert "api" in ev.evidence


def test_service_health_degraded_or_unknown_is_unproven():
    assert _eval("service_health", {"services": [_svc("degraded")]}).status is Status.UNPROVEN
    assert _eval("service_health", {"services": [_svc("unknown")]}).status is Status.UNPROVEN


def test_service_health_healthy_without_latency_evidence_is_unproven():
    """'healthy' with no observed latency is a claim, not an effect."""
    ev = _eval("service_health", {"services": [_svc("healthy", latency=None)]})
    assert ev.status is Status.UNPROVEN


def test_service_health_empty_is_unproven():
    assert _eval("service_health", {"services": []}).status is Status.UNPROVEN


# ------------------------------------------------------------------- backup


def _backup(size=99_600_000, artifact="/backups/x.dump", created=None):
    return {
        "artifact": artifact,
        "size_bytes": size,
        "created_at": (created or FRESH).isoformat(),
    }


def test_backup_with_artifact_size_and_fresh_created_at_is_green():
    ev = _eval("backup", _backup(), cadence=CADENCE_DAILY)
    assert ev.status is Status.GREEN
    assert "size_bytes" in ev.evidence


def test_backup_zero_bytes_is_not_green():
    ev = _eval("backup", _backup(size=0), cadence=CADENCE_DAILY)
    assert ev.status is Status.UNPROVEN


def test_backup_error_is_failed():
    ev = _eval("backup", {"error": "pg_dump: connection refused"}, cadence=CADENCE_DAILY)
    assert ev.status is Status.FAILED
    assert "connection refused" in ev.evidence


def test_backup_missing_evidence_fields_is_unproven():
    ev = _eval("backup", {"artifact": "/backups/x.dump"}, cadence=CADENCE_DAILY)
    assert ev.status is Status.UNPROVEN


def test_backup_old_created_at_is_stale_even_if_report_is_fresh():
    """A freshly pushed report about an old dump must not read green —
    the dump's age is the effect that matters."""
    old_dump = NOW - timedelta(days=4)
    ev = _eval("backup", _backup(created=old_dump), cadence=CADENCE_DAILY)
    assert ev.status is Status.STALE


# --------------------------------------------------------- backup_validate


def _checks(**overrides):
    base = {
        "backup_exists": "PASS",
        "backup_fresh": "PASS",
        "backup_nonempty": "PASS",
        "backup_toc": "PASS",
        "restore_live": "PASS",
    }
    base.update(overrides)
    return {"checks": [{"name": k, "status": v} for k, v in base.items()]}


def test_backup_validate_all_pass_including_restore_is_green():
    ev = _eval("backup_validate", _checks(), cadence=CADENCE_DAILY)
    assert ev.status is Status.GREEN
    assert "restore_live" in ev.evidence


def test_backup_validate_warn_is_unproven_not_failed():
    """The producer's WARN/FAIL split means unproven-vs-broken; the
    console preserves it verbatim (spec §4)."""
    ev = _eval("backup_validate", _checks(restore_live="WARN"), cadence=CADENCE_DAILY)
    assert ev.status is Status.UNPROVEN


def test_backup_validate_fail_is_failed():
    ev = _eval("backup_validate", _checks(backup_toc="FAIL"), cadence=CADENCE_DAILY)
    assert ev.status is Status.FAILED
    assert "backup_toc" in ev.evidence


def test_backup_validate_without_restore_check_is_unproven():
    payload = _checks()
    payload["checks"] = [c for c in payload["checks"] if c["name"] != "restore_live"]
    ev = _eval("backup_validate", payload, cadence=CADENCE_DAILY)
    assert ev.status is Status.UNPROVEN


# ------------------------------------------------------------------- canary


def test_canary_green_with_smoke_results_is_green():
    ev = _eval("canary", {"status": "green", "smoke_results": {"tier1": "pass"}})
    assert ev.status is Status.GREEN


def test_canary_green_without_smoke_results_is_unproven():
    ev = _eval("canary", {"status": "green", "smoke_results": {}})
    assert ev.status is Status.UNPROVEN


@pytest.mark.parametrize("status", ["red", "rollback"])
def test_canary_red_or_rollback_is_failed(status):
    ev = _eval("canary", {"status": status, "smoke_results": {"tier1": "fail"}})
    assert ev.status is Status.FAILED


# ----------------------------------------------------------------- versions


def test_versions_with_content_is_green_presence_is_the_effect():
    ev = _eval("versions", {"versions": {"axiom-os-lm": "0.57.0"}})
    assert ev.status is Status.GREEN


def test_versions_empty_is_unproven():
    ev = _eval("versions", {"versions": {}})
    assert ev.status is Status.UNPROVEN


# ------------------------------------------------------------ unknown kinds


def test_unknown_kind_is_refused_not_guessed():
    with pytest.raises(ValueError):
        _eval("weather", {})


# ------------------------------------------------------------------- rollup


def test_rollup_worst_wins():
    assert rollup([Status.GREEN, Status.UNPROVEN, Status.FAILED]) is Status.FAILED
    assert rollup([Status.GREEN, Status.STALE]) is Status.STALE
    assert rollup([Status.GREEN, Status.UNPROVEN]) is Status.UNPROVEN
    assert rollup([Status.GREEN, Status.GREEN]) is Status.GREEN


def test_rollup_of_nothing_is_unknown_never_green():
    assert rollup([]) is Status.UNKNOWN


# --- Plain language (founder feedback 2026-09-24: "too low level") ---


def test_stale_evidence_reads_as_a_sentence_not_a_log_line():
    """This string is shown to a person on a case page. It used to read
    'last report 2026-09-24T17:52:59.748356+00:00 is 7990s old, past 3x
    cadence (900s)' — three machine units and a microsecond timestamp."""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
    ev = evaluate_report(
        kind="heartbeat",
        payload={},
        received_at=now - timedelta(hours=2),
        cadence_seconds=900,
        now=now,
    )
    assert ev.status is Status.STALE
    assert ev.evidence == (
        "The last report arrived 2 hours ago. This node reports every 15 minutes."
    )
    for machine in ("cadence", "3x", "+00:00", "s old"):
        assert machine not in ev.evidence


def test_a_short_silence_still_reads_in_minutes():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
    ev = evaluate_report(
        kind="heartbeat",
        payload={},
        received_at=now - timedelta(minutes=50),
        cadence_seconds=900,
        now=now,
    )
    assert "50 minutes ago" in ev.evidence
    assert "every 15 minutes" in ev.evidence


def test_an_odd_cadence_is_still_said_in_words():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
    ev = evaluate_report(
        kind="heartbeat",
        payload={},
        received_at=now - timedelta(days=5),
        cadence_seconds=86_400,
        now=now,
    )
    assert "5 days ago" in ev.evidence
    assert "every day" in ev.evidence
