# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reports-vs-Reality: how long can a silent failure hide?

A corpus of REAL, dated incidents from this platform's own production
history — each one a case where conventional signals (service state,
HTTP health, heartbeat files, 2xx acceptance) read healthy while the
effect was absent. Three baseline detectors judge each incident from
exactly the signals that existed at the time; the console detector
applies the fleet console's effect-checked bindings (the shipped
`evaluate_report` wherever the incident maps to a shipped report kind,
the evidence-or-nothing principle where the collector is still P2 —
each row says which).

Honesty constraints, enforced by tests/bench/test_reports_vs_reality.py:
the console column is NOT all-catches by construction (a healthy
control must render green), baselines see every signal they actually
had, and every headline number a paper cites comes from ``run_bench()``.

Run: ``python -m axiom.bench.reports_vs_reality`` → markdown table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from axiom.extensions.builtins.fleet.status import Status, evaluate_report

_NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Incident:
    name: str
    observed_on: str          # date the incident is documented
    description: str
    hidden_for_hours: float   # how long it actually went undetected
    cadence_hours: float      # the effect's declared cadence
    actually_broken: bool
    #: signals the operator's existing tooling showed at the time
    signals: dict = field(default_factory=dict)
    #: fleet-report shape for the console detector: kind + payload +
    #: report age (hours) relative to cadence
    report_kind: str | None = None
    report_payload: dict = field(default_factory=dict)
    report_age_hours: float = 0.0
    #: "shipped" = judged by the shipped evaluate_report binding;
    #: "principle" = the collector is P2, judged by evidence-or-nothing
    binding: str = "shipped"
    exposure_note: str = ""


CORPUS: tuple[Incident, ...] = (
    Incident(
        name="scheduled-dispatch-authz-expiry",
        observed_on="2026-09-19..21",
        description=(
            "Nightly backup dispatch floor-denied after the host outlived "
            "its capability TTL; service active, timer firing, fire-log "
            "rows written — no new dump for three days."
        ),
        hidden_for_hours=63,
        cadence_hours=24,
        actually_broken=True,
        signals={"service_active": True, "timer_fired": True, "heartbeat_file_fresh": True},
        report_kind="backup",
        # Judged at the automatic bound: had nobody read the evidence line
        # early (a human did, at 63h), the dump crosses 3x cadence and the
        # row flips STALE with no one watching — that flip is the claim
        # under test. The 63h figure stays as the ACTUAL time hidden.
        report_payload={
            "artifact": "/backups/nightly.dump",
            "size_bytes": 99_603_889,
            "created_at": (_NOW - timedelta(hours=73)).isoformat(),
        },
        report_age_hours=0.2,
        exposure_note=(
            "RPO breach: 3 days of unprotected data vs a 1-day policy; "
            "recovery exposure = 3 days of re-ingest + reconstruction."
        ),
    ),
    Incident(
        name="ingest-accepts-rows-nobody-lands",
        observed_on="2026-09-21 (running ≥30 days)",
        description=(
            "Every push connector registered without attribution: rows "
            "accepted with 2xx forever, conformance silently skipped all "
            "of them — the pipeline looked healthiest while delivering "
            "nothing downstream."
        ),
        hidden_for_hours=720,
        cadence_hours=24,
        actually_broken=True,
        signals={"service_active": True, "accepts_writes_2xx": True},
        report_kind=None,  # P2 collector: rows-LANDED evidence
        report_payload={"rows_landed": 0, "rows_accepted": 41_000},
        binding="principle",
        exposure_note=(
            "Pure waste: ≥30 days of paid ingest compute + storage carrying "
            "rows that never reached the serving tier (a prior sweep found "
            "hundreds of GB of such dead weight), plus partner rework."
        ),
    ),
    Incident(
        name="heartbeat-written-by-dead-agent",
        observed_on="2026-06-01",
        description=(
            "A background agent hung for 28 hours while its heartbeat "
            "file kept being written — the liveness signal outlived the "
            "work it was supposed to prove."
        ),
        hidden_for_hours=28,
        cadence_hours=0.25,
        actually_broken=True,
        signals={"service_active": True, "heartbeat_file_fresh": True},
        report_kind="heartbeat",
        report_payload={},
        report_age_hours=28,  # a dead worker stops PUSHING; the file is not the report
        exposure_note=(
            "28 hours of a paid always-on host doing nothing, plus every "
            "downstream task that silently queued behind it."
        ),
    ),
    Incident(
        name="watcher-silent-fourteen-days",
        observed_on="2026-06 (motivated ADR-037)",
        description=(
            "The hygiene agent went silent for 14 days; nothing watched "
            "the watcher, so its absence produced no signal at all."
        ),
        hidden_for_hours=336,
        cadence_hours=0.25,
        actually_broken=True,
        signals={"service_active": True},
        report_kind="heartbeat",
        report_payload={},
        report_age_hours=336,
        exposure_note=(
            "Two weeks of drift, leak and CI-health surveillance not "
            "happening; the cleanup cost arrives later as one large bill."
        ),
    ),
    Incident(
        name="canary-dead-three-nights",
        observed_on="2026-09-17..19",
        description=(
            "The nightly canary update never ran (its resolver was never "
            "deployed and its alert function was defined below its use); "
            "the timer stayed green all three nights."
        ),
        hidden_for_hours=72,
        cadence_hours=24,
        actually_broken=True,
        signals={"service_active": True, "timer_fired": True},
        report_kind="canary",
        report_payload={},
        report_age_hours=72,  # no fresh attestation is the evidence of absence
        exposure_note=(
            "Three nights of the fleet's early-warning channel dark: any "
            "bad platform release would have promoted with zero canary "
            "coverage."
        ),
    ),
    Incident(
        name="backup-armed-but-dsn-unresolvable",
        observed_on="2026-09-18",
        description=(
            "Arming reported true while the skill could not resolve its "
            "database DSN — the nightly would have failed every night "
            "with 'armed' on every status screen."
        ),
        hidden_for_hours=24,
        cadence_hours=24,
        actually_broken=True,
        signals={"service_active": True, "self_report_ok": True},
        report_kind="backup_validate",
        report_payload={
            "checks": [
                {"name": "backup_exists", "status": "PASS"},
                {"name": "restore_live", "status": "WARN"},
            ]
        },
        report_age_hours=0.2,
        exposure_note=(
            "The armed-but-broken state converts every future night into "
            "silent RPO breach until someone restores by hand and finds "
            "nothing to restore from."
        ),
    ),
    Incident(
        name="front-door-serves-stale-build",
        observed_on="2026-09-19",
        description=(
            "The public entry point served a stale build (the update path "
            "could not escalate under its own hardening); HTTP 200 on "
            "every probe, wrong content behind it."
        ),
        hidden_for_hours=48,
        cadence_hours=24,
        actually_broken=True,
        signals={"service_active": True, "http_health_200": True},
        report_kind=None,  # P2: versions/drift view (declared vs deployed)
        report_payload={"declared_version": "1.6.70", "deployed_version": "1.6.62"},
        binding="principle",
        exposure_note=(
            "Every user of the front door ran two-releases-old behavior "
            "for two days — including fixes they had been told were live."
        ),
    ),
)


HEALTHY_CONTROL = Incident(
    name="healthy-node-control",
    observed_on="2026-09-21",
    description="A node pushing on cadence with real evidence — must render green.",
    hidden_for_hours=0,
    cadence_hours=24,
    actually_broken=False,
    signals={"service_active": True, "http_health_200": True, "heartbeat_file_fresh": True},
    report_kind="backup",
    report_payload={
        "artifact": "/backups/nightly.dump",
        "size_bytes": 110_961_971,
        "created_at": (_NOW - timedelta(hours=6)).isoformat(),
    },
    report_age_hours=0.2,
)


@dataclass(frozen=True)
class BenchVerdict:
    caught: bool
    detail: str
    detect_bound_hours: float | None = None


class ConsoleDetector:
    """Effect-checked judgment: shipped bindings where the kind exists,
    evidence-or-nothing where the collector is P2."""

    label = "fleet console (effect-checked)"

    def judge(self, row: Incident) -> BenchVerdict:
        bound = 3 * row.cadence_hours
        if row.report_kind is not None:
            evaluation = evaluate_report(
                kind=row.report_kind,
                payload=row.report_payload,
                received_at=_NOW - timedelta(hours=row.report_age_hours),
                cadence_seconds=int(row.cadence_hours * 3600),
                now=_NOW,
            )
            caught = evaluation.status is not Status.GREEN
            return BenchVerdict(
                caught=caught,
                detail=f"{evaluation.status.value}: {evaluation.evidence}",
                detect_bound_hours=bound if caught else None,
            )
        # P2 principle: a claim must carry its effect evidence.
        payload = row.report_payload
        effect_present = bool(payload.get("rows_landed")) or (
            payload.get("declared_version") is not None
            and payload.get("declared_version") == payload.get("deployed_version")
        )
        caught = not effect_present
        return BenchVerdict(
            caught=caught,
            detail="evidence-or-nothing (P2 collector)",
            detect_bound_hours=bound if caught else None,
        )


class _Baseline:
    label = "baseline"
    signal = ""
    #: incident names this baseline was watching AND called healthy
    historically_missed: frozenset = frozenset()

    def applicable(self, row: Incident) -> bool:
        return self.signal in row.signals

    def judge(self, row: Incident) -> BenchVerdict:
        if not self.applicable(row):
            return BenchVerdict(caught=False, detail="not watching")
        healthy = bool(row.signals[self.signal])
        return BenchVerdict(caught=not healthy, detail=f"{self.signal}={healthy}")


class BaselineServiceState(_Baseline):
    label = "service state (systemd)"
    signal = "service_active"
    historically_missed = frozenset(r.name for r in CORPUS)


class BaselineHttpHealth(_Baseline):
    label = "HTTP health probe"
    signal = "http_health_200"
    historically_missed = frozenset({"front-door-serves-stale-build"})


class BaselineHeartbeatFile(_Baseline):
    label = "heartbeat file freshness"
    signal = "heartbeat_file_fresh"
    historically_missed = frozenset(
        {"heartbeat-written-by-dead-agent", "scheduled-dispatch-authz-expiry"}
    )


def run_bench() -> dict:
    baselines = (BaselineServiceState(), BaselineHttpHealth(), BaselineHeartbeatFile())
    console = ConsoleDetector()
    rows = []
    console_catches = 0
    baseline_catches = {b.label: 0 for b in baselines}
    for row in CORPUS:
        cv = console.judge(row)
        console_catches += cv.caught
        caught_by = []
        for b in baselines:
            bv = b.judge(row)
            if bv.caught:
                baseline_catches[b.label] += 1
                caught_by.append(b.label)
        rows.append(
            {
                "incident": row.name,
                "observed_on": row.observed_on,
                "hidden_for_hours": row.hidden_for_hours,
                "console_caught": cv.caught,
                "console_detail": cv.detail,
                "console_bound_hours": cv.detect_bound_hours,
                "binding": row.binding,
                "baseline_caught_by": caught_by,
                "exposure_note": row.exposure_note,
            }
        )
    hidden = sorted(r.hidden_for_hours for r in CORPUS)
    median_hidden = hidden[len(hidden) // 2]
    summary = {
        "corpus_size": len(CORPUS),
        "console_catch_rate": console_catches / len(CORPUS),
        "best_baseline_catch_rate": max(baseline_catches.values()) / len(CORPUS),
        "median_hidden_hours": median_hidden,
        "worst_hidden_hours": hidden[-1],
    }
    return {"rows": rows, "summary": summary}


def main() -> int:
    table = run_bench()
    s = table["summary"]
    print("# Reports vs Reality — how long can a silent failure hide?\n")
    print(
        f"Corpus: {s['corpus_size']} real, dated production incidents. "
        f"Console catch rate: {s['console_catch_rate']:.0%}. Best baseline: "
        f"{s['best_baseline_catch_rate']:.0%}. Median time hidden: "
        f"{s['median_hidden_hours']:.0f}h; worst: {s['worst_hidden_hours']:.0f}h.\n"
    )
    print("| incident | observed | hidden (h) | console | bound (h) | binding | baselines that caught it |")
    print("|---|---|---:|---|---:|---|---|")
    for r in table["rows"]:
        print(
            f"| {r['incident']} | {r['observed_on']} | {r['hidden_for_hours']:.0f} "
            f"| {'CAUGHT' if r['console_caught'] else 'missed'} "
            f"| {r['console_bound_hours'] or '—'} | {r['binding']} "
            f"| {', '.join(r['baseline_caught_by']) or 'none'} |"
        )
    print("\nHealthy control renders green (the bench can fail): ", end="")
    print("PASS" if not ConsoleDetector().judge(HEALTHY_CONTROL).caught else "RIGGED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
