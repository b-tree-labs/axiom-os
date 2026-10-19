# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Governance-overhead benchmark: what do the gate + receipt + audit layers cost?

Measures the per-action wall-time and allocation overhead of Axiom's
governance seams versus the same action body with governance stubbed out.
The number reported is the DELTA (governed minus bare), not an absolute.

Seams measured (the real ones, not mocks):

1. ``guard``        — ``axiom.policy.agent_action_guard.guarded_act``: the
                      policy action guard (hard-disable env check, pause
                      sentinel stat, reversibility, volume bound) plus its
                      decision-provenance journal write through
                      ``axiom.policy.action_ledger`` (JSONL backend,
                      locked append + fsync).
2. ``gate_memory``  — ``ApprovalGate`` with the in-memory store:
                      submit (WRITE → pending) + approve. Mechanical gate
                      cost only; human decision latency is excluded by design.
3. ``gate_durable`` — ``ApprovalGate`` with the durable ``FileActionStore``
                      (LockedJsonFile: flock + read + atomic tempfile
                      replace + fsync, twice — once for submit, once for
                      approve). The store is re-seeded to a fixed 8-entry
                      queue between iterations, OUTSIDE the timed region,
                      so the numbers reflect a steady-state small queue
                      rather than an unboundedly growing file.
4. ``receipt``      — ``CompositionService.write``: memory-fragment receipt
                      with (T,U,A,R) provenance, ADR-035 accountability
                      validation, ownership, policy scope resolution,
                      Ed25519 signing, SQLite artifact persistence, and the
                      signed JSONL audit-log append.
5. ``governed_full``— the composed path: guard → approval gate (durable) →
                      action body → receipt. This is the honest "all layers
                      on" number.

Baseline (``bare``) is the identical action body (build a small result dict
+ JSON-serialize it) with none of the above.

Methodology: warm-up iterations discarded; N >= 1000 timed iterations per
variant (per-iteration ``time.perf_counter_ns``); p50/p95/p99/mean reported;
deltas are computed percentile-by-percentile against ``bare``. Allocation
overhead is measured in a separate tracemalloc pass (tracemalloc slows
execution, so it never shares a pass with the timing run): per-iteration
incremental traced-memory peak, median over N_alloc iterations.

Honesty labels: single machine, single process, single seed, one run.
See the companion results doc for the limitations section.

Reproduce:

    PYTHONPATH=src python scripts/bench_governance_overhead.py \
        --out docs/working/governance-overhead-bench-2026-09-19.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

# The benchmark journals to the JSONL action-ledger backend explicitly so a
# reachable-or-not Postgres on the host cannot change what is being measured.
# This is the common single-node posture; the SQL backend is a different
# (unmeasured) deployment shape. Set before axiom imports.
os.environ["AXIOM_ACTION_LEDGER_BACKEND"] = "jsonl"
# Deterministic run: no HMAC chaining on the action ledger (the default
# posture when AXIOM_AUDIT_HMAC_KEY is unset). Recorded in the output.
_HMAC_WAS_SET = "AXIOM_AUDIT_HMAC_KEY" in os.environ
os.environ.pop("AXIOM_AUDIT_HMAC_KEY", None)

SEED = 20260919  # single-seed run; label carried into the results JSON


# ---------------------------------------------------------------------------
# Representative action body (identical in every variant)
# ---------------------------------------------------------------------------

_PAYLOAD = {"target": "resource-42", "op": "update", "value": 7}


def _action_body(candidate) -> bool:
    """The governed 'work': build a result record and serialize it."""
    result = {
        "candidate": str(candidate),
        "outcome": "ok",
        "detail": "representative-action-result " + "x" * 40,
    }
    json.dumps(result)
    return True


# ---------------------------------------------------------------------------
# Variant construction
# ---------------------------------------------------------------------------


def _make_composition_service(root: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    kp = generate_keypair()
    return CompositionService(
        artifact_registry=ArtifactRegistry(backend=SQLiteBackend(root / "artifacts.db")),
        audit_log=AuditLog(root / "audit.jsonl", signing_keypair=kp),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def _write_receipt(svc, candidate) -> None:
    svc.write(
        content={
            "action": "bench_governed_op",
            "candidate": str(candidate),
            "outcome": "ok",
            "event_time": datetime.now(UTC).isoformat(),
        },
        cognitive_type="episodic",
        principal_id="@ben.booth",
        agents={"@bench-agent"},
        resources={"bench"},
        accountable_human_id="@ben.booth",
        session_id="bench-session",
    )


def build_variants(work: Path):
    """Return {name: (callable, between_iterations_callable_or_None)}."""
    from axiom.infra.orchestrator.actions import Action
    from axiom.infra.orchestrator.approval import ApprovalGate
    from axiom.infra.orchestrator.approval_store import (
        FileActionStore,
        InMemoryActionStore,
    )
    from axiom.infra.state import locked_append_jsonl
    from axiom.policy.agent_action_guard import AgentAction, guarded_act

    variants: dict = {}

    # -- bare -------------------------------------------------------------
    variants["bare"] = (lambda: _action_body(_PAYLOAD), None)

    # -- journal_append (attribution micro-seam) ---------------------------
    # One locked+fsynced JSONL append of a representative record: the
    # primitive under both the action ledger and most audit files. Isolates
    # how much of the guard/receipt cost is the durability fsync itself.
    journal_path = work / "journal" / "bench.jsonl"

    def run_journal_append():
        _action_body(_PAYLOAD)
        locked_append_jsonl(journal_path, {"ts": time.time(), "candidate": "resource-42",
                                           "outcome": "ok"})

    variants["journal_append"] = (run_journal_append, None)

    # -- guard ------------------------------------------------------------
    guard_state = work / "guard-state"
    guard_state.mkdir(parents=True, exist_ok=True)

    def run_guard():
        action = AgentAction(
            agent="bench",
            op_class="bench.op",
            name="bench_overhead",
            candidates=[_PAYLOAD],
        )
        guarded_act(action, do_one=_action_body, state_dir=guard_state)

    variants["guard"] = (run_guard, None)

    # -- gate (in-memory store) -------------------------------------------
    gate_mem = ApprovalGate(store=InMemoryActionStore())

    def run_gate_memory():
        a = gate_mem.submit(Action(name="bench_governed_op", params={"p": 1}))
        gate_mem.approve(a.action_id, decided_by="@ben.booth")
        _action_body(_PAYLOAD)

    variants["gate_memory"] = (run_gate_memory, None)

    # -- gate (durable file store) ----------------------------------------
    durable_path = work / "gate-durable" / "approvals.json"
    durable_path.parent.mkdir(parents=True, exist_ok=True)
    gate_dur = ApprovalGate(store=FileActionStore(durable_path))
    for i in range(8):  # steady-state queue of 8 pending actions
        gate_dur.submit(Action(name=f"seed_{i}", params={}))
    _seed_bytes = durable_path.read_bytes()

    def reseed_durable():
        durable_path.write_bytes(_seed_bytes)

    def run_gate_durable():
        a = gate_dur.submit(Action(name="bench_governed_op", params={"p": 1}))
        gate_dur.approve(a.action_id, decided_by="@ben.booth")
        _action_body(_PAYLOAD)

    variants["gate_durable"] = (run_gate_durable, reseed_durable)

    # -- receipt (CompositionService.write) --------------------------------
    receipt_root = work / "receipt"
    receipt_root.mkdir(parents=True, exist_ok=True)
    svc_receipt = _make_composition_service(receipt_root)

    def run_receipt():
        _action_body(_PAYLOAD)
        _write_receipt(svc_receipt, _PAYLOAD)

    variants["receipt"] = (run_receipt, None)

    # -- governed_full: guard -> durable gate -> body -> receipt -----------
    full_root = work / "full"
    full_root.mkdir(parents=True, exist_ok=True)
    svc_full = _make_composition_service(full_root)
    full_gate_path = full_root / "approvals.json"
    gate_full = ApprovalGate(store=FileActionStore(full_gate_path))
    for i in range(8):
        gate_full.submit(Action(name=f"seed_{i}", params={}))
    _full_seed_bytes = full_gate_path.read_bytes()
    full_guard_state = full_root / "guard-state"
    full_guard_state.mkdir(parents=True, exist_ok=True)

    def reseed_full():
        full_gate_path.write_bytes(_full_seed_bytes)

    def governed_do_one(candidate) -> bool:
        a = gate_full.submit(
            Action(name="bench_governed_op", params={"candidate": str(candidate)})
        )
        gate_full.approve(a.action_id, decided_by="@ben.booth")
        ok = _action_body(candidate)
        _write_receipt(svc_full, candidate)
        return ok

    def run_full():
        action = AgentAction(
            agent="bench",
            op_class="bench.op",
            name="bench_overhead_full",
            candidates=[_PAYLOAD],
        )
        guarded_act(action, do_one=governed_do_one, state_dir=full_guard_state)

    variants["governed_full"] = (run_full, reseed_full)

    return variants


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def time_variant(fn, between, *, warmup: int, iterations: int) -> list[int]:
    """Per-iteration wall time in nanoseconds. Warm-up discarded."""
    for _ in range(warmup):
        if between:
            between()
        fn()
    samples: list[int] = []
    for _ in range(iterations):
        if between:
            between()  # untimed: restores steady-state fixtures
        t0 = time.perf_counter_ns()
        fn()
        samples.append(time.perf_counter_ns() - t0)
    return samples


def alloc_variant(fn, between, *, warmup: int, iterations: int) -> list[int]:
    """Per-iteration incremental traced-memory peak in bytes (tracemalloc)."""
    for _ in range(warmup):
        if between:
            between()
        fn()
    samples: list[int] = []
    tracemalloc.start()
    try:
        for _ in range(iterations):
            if between:
                between()
            tracemalloc.reset_peak()
            base = tracemalloc.get_traced_memory()[0]
            fn()
            peak = tracemalloc.get_traced_memory()[1]
            samples.append(max(0, peak - base))
    finally:
        tracemalloc.stop()
    return samples


def summarize_ns(samples: list[int]) -> dict:
    s = sorted(samples)

    def pct(p: float) -> float:
        idx = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
        return s[idx] / 1000.0  # ns -> us

    return {
        "n": len(s),
        "p50_us": round(pct(50), 2),
        "p95_us": round(pct(95), 2),
        "p99_us": round(pct(99), 2),
        "mean_us": round(statistics.fmean(s) / 1000.0, 2),
        "min_us": round(s[0] / 1000.0, 2),
        "max_us": round(s[-1] / 1000.0, 2),
    }


def summarize_alloc(samples: list[int]) -> dict:
    s = sorted(samples)
    return {
        "n": len(s),
        "peak_median_kib": round(statistics.median(s) / 1024.0, 2),
        "peak_p95_kib": round(s[min(len(s) - 1, round(0.95 * (len(s) - 1)))] / 1024.0, 2),
    }


def host_info() -> dict:
    cpu = ""
    if sys.platform == "darwin":
        try:
            cpu = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except OSError:
            cpu = ""
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": cpu or platform.processor(),
        "python": sys.version.split()[0],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iterations", type=int, default=1000,
                    help="timed iterations per variant (min 1000)")
    ap.add_argument("--warmup", type=int, default=50,
                    help="discarded warm-up iterations per variant")
    ap.add_argument("--alloc-iterations", type=int, default=200,
                    help="iterations for the tracemalloc allocation pass")
    ap.add_argument("--state-dir", type=Path, default=Path.home() / ".axi",
                    help="where the capability telemetry series lives; supplies "
                         "the MEASURED denominator for the overhead claim")
    ap.add_argument("--out", type=Path, default=None,
                    help="write results JSON here (default: stdout only)")
    args = ap.parse_args(argv)
    iterations = max(1000, args.iterations)

    results: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "host": host_info(),
        "methodology": {
            "iterations": iterations,
            "warmup_discarded": args.warmup,
            "alloc_iterations": args.alloc_iterations,
            "timer": "time.perf_counter_ns per iteration",
            "allocation": "tracemalloc incremental peak per iteration, separate pass",
            "delta": "percentile-by-percentile vs the bare action body",
            "seed": SEED,
            "honesty_labels": [
                "single machine", "single process", "single seed", "one run",
                "action-ledger backend forced to JSONL",
                "HMAC chaining disabled (AXIOM_AUDIT_HMAC_KEY unset)"
                + (" — WAS set in caller env, cleared for determinism" if _HMAC_WAS_SET else ""),
                "durable approval queue re-seeded to 8 entries between iterations (untimed)",
                "human approval latency excluded by design (mechanical gate cost only)",
            ],
        },
        "variants": {},
        "deltas_vs_bare": {},
        "context": {},
    }

    with tempfile.TemporaryDirectory(prefix="axiom-gov-bench-") as tmp:
        variants = build_variants(Path(tmp))

        timing: dict[str, dict] = {}
        for name, (fn, between) in variants.items():
            print(f"[timing] {name}: {iterations} iterations ...", file=sys.stderr)
            samples = time_variant(fn, between, warmup=args.warmup, iterations=iterations)
            timing[name] = summarize_ns(samples)

        alloc: dict[str, dict] = {}
        for name, (fn, between) in variants.items():
            print(f"[alloc]  {name}: {args.alloc_iterations} iterations ...", file=sys.stderr)
            samples = alloc_variant(
                fn, between, warmup=10, iterations=args.alloc_iterations
            )
            alloc[name] = summarize_alloc(samples)

    for name in timing:
        results["variants"][name] = {**timing[name], "alloc": alloc[name]}

    bare = timing["bare"]
    bare_alloc = alloc["bare"]
    for name in timing:
        if name == "bare":
            continue
        results["deltas_vs_bare"][name] = {
            "p50_us": round(timing[name]["p50_us"] - bare["p50_us"], 2),
            "p95_us": round(timing[name]["p95_us"] - bare["p95_us"], 2),
            "p99_us": round(timing[name]["p99_us"] - bare["p99_us"], 2),
            "mean_us": round(timing[name]["mean_us"] - bare["mean_us"], 2),
            "alloc_peak_median_kib": round(
                alloc[name]["peak_median_kib"] - bare_alloc["peak_median_kib"], 2
            ),
        }

    full_p50 = results["deltas_vs_bare"]["governed_full"]["p50_us"]
    results["context"] = {
        "note": "percent overhead if the governed action's real work were a "
                "typical tool call (~100 ms) or a typical LLM call (~1 s). "
                "These denominators are ASSUMED. See `observed` for the one "
                "this install actually measured.",
        "denominator": "assumed",
        "governed_full_p50_pct_of_100ms_tool_call": round(full_p50 / 100_000 * 100, 3),
        "governed_full_p50_pct_of_1s_llm_call": round(full_p50 / 1_000_000 * 100, 4),
    }

    # The measured denominator. "~100 ms" above is a number nobody took; the
    # capability series holds what calls on this install actually cost. When
    # there are enough of them, the overhead claim stops resting on an
    # assumption. When there are not, this says so rather than quietly leaving
    # the assumed figure to be read as measured.
    try:
        from axiom.infra.capability_telemetry import governance_share, observed_latency

        observed = observed_latency(state_dir=args.state_dir)
        results["observed"] = {
            "state_dir": str(args.state_dir),
            "latency": observed,
            "governed_full": governance_share(overhead_us=full_p50, observed=observed),
        }
    except Exception as exc:  # noqa: BLE001 — the benchmark stands without it
        results["observed"] = {"error": f"{type(exc).__name__}: {exc}"}

    text = json.dumps(results, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
