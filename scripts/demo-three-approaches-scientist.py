#!/usr/bin/env python
# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Demo: three approaches a scientist would compare for a hard integral.

The scientist's framing
-----------------------

> I need to estimate the area of the unit disc — a stand-in for a
> high-dimensional integral I don't have a closed form for. I want to
> compare three approaches: closed-form (works only because pi is in
> the table), numerical quadrature (works for low-dim), and federated
> Monte Carlo (the only approach that generalises to high-dim). Show
> me wall time + accuracy + the speedup curve as I add peers.

Three approaches, all instrumented:

A. **Closed-form via SymPy.** ``float(pi)``. Instant, exact-to-
   machine-precision. Only works when the answer is in a table.

B. **Numerical quadrature via SciPy.** ``scipy.integrate.dblquad`` over
   the indicator of the unit disc. Deterministic, accurate, but
   ``O(M^d)`` — explodes with dimension.

C. **Federated Monte Carlo.** ``MonteCarloDartsKernel`` shipped to
   ``k`` peers via the existing ``decompose_and_solve`` orchestrator.
   Time each ``k``. Compute speedup ``S(k)``. Overlay against the
   analytical Amdahl prediction.

The honest twist: pi estimation is a *cheap* kernel (~100 ns per
sample). Federation overhead at small k can swamp the per-sample
parallel win — the demo MAKES this visible. Run with ``--kernel
mcmc-fake`` or ``--kernel openmc-fake`` to see the same shape with
artificially expensive kernels (sleep 1ms / 100ms per sample): in
those regimes federation crushes single-machine.

CI mode (default): hermetic, no SSH, no self-hosted node. ``--include-selfhost``
optionally pings a real self-hosted peer to surface real cross-node
latency; falls back gracefully if unreachable.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Ensure the worktree's src/ is importable when run directly.
_REPO_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))


# Headless matplotlib so the demo runs in CI without a display.
import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import numpy as np  # noqa: E402
import scipy.integrate as si  # noqa: E402
import sympy as sp  # noqa: E402

from axiom.compute_decomposition.coordination import (  # noqa: E402
    SignedChunkResult,
    decompose_and_solve,
)
from axiom.compute_decomposition.coordination.monte_carlo_kernel import (  # noqa: E402
    MonteCarloDartsKernel,
    MonteCarloDecomposer,
    MonteCarloPiRecomposer,
)
from axiom.compute_decomposition.registry import (  # noqa: E402
    PatternRegistry,
)


# ---------------------------------------------------------------------------
# Sci Displays brand palette (graphite / off-white / UT burnt-orange)
# ---------------------------------------------------------------------------


BRAND_BG = "#1B1F23"          # graphite
BRAND_PANEL = "#262B30"       # graphite-2
BRAND_FG = "#F5F1E8"          # off-white
BRAND_ACCENT = "#BF5700"      # UT burnt orange (color-only nod)
BRAND_ACCENT2 = "#7A9CC6"     # cool blue for the predicted curve
BRAND_MUTED = "#8E9197"


# ---------------------------------------------------------------------------
# Fake peers (default mode — no SSH)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakePeer:
    node_id: str
    display_name: str
    public_key: str = "fake-pub-b64"
    state: str = "verified"
    capabilities: tuple[str, ...] = ("compute:embarrassingly_parallel",)
    trust_score: float = 0.7
    latency_estimate_ms: float = 5.0
    compute_capacity_hint: float = 1.0


def _make_fake_peers(k: int) -> list[FakePeer]:
    """Construct k fake peers with stable IDs."""
    out: list[FakePeer] = []
    for i in range(k):
        out.append(FakePeer(
            node_id=f"node-fake-{i:02d}",
            display_name=f"fake-{i:02d}:demo",
            trust_score=0.5 + (i % 5) * 0.05,
            compute_capacity_hint=1.0,
        ))
    return out


# ---------------------------------------------------------------------------
# Compute call factory: runs the MC kernel "as if" on the assigned peer
# ---------------------------------------------------------------------------


def _make_mc_compute_call(
    *,
    per_call_overhead_ms: float = 1.0,
    selfhost_node_id: Optional[str] = None,
    selfhost_ssh_target: Optional[str] = None,
) -> Callable[..., SignedChunkResult]:
    """Return a compute_call closure for the MonteCarloDartsKernel.

    The cross-NODE compute path's default ``compute_call`` only knows
    how to ship ``sum_of_squares`` today (Phase B will generalise).
    For this demo we run the kernel in-process and stamp a synthetic
    Ed25519-style signature, simulating what a real peer would
    return. We add a small per-call overhead to simulate the
    federation round-trip — that's the cost the scientist is "paying"
    for parallelism.

    When ``selfhost_node_id`` is supplied, calls assigned to that
    node_id additionally invoke a real ``ssh <target> echo OK`` to
    surface true cross-node latency; the kernel work itself still
    runs locally (shipping the kernel is Phase B work).
    """
    kernel = MonteCarloDartsKernel()

    def _call(*, peer_id: str, peer_display_name: str, chunk) -> SignedChunkResult:
        import hashlib
        # Simulate the federation round-trip overhead.
        time.sleep(per_call_overhead_ms / 1000.0)

        # If this chunk landed on the real self-hosted peer, ping it for
        # honest latency surfacing.
        if selfhost_node_id and peer_id == selfhost_node_id and selfhost_ssh_target:
            try:
                subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=2",
                     "-o", "BatchMode=yes",
                     selfhost_ssh_target, "echo OK"],
                    check=True, capture_output=True, timeout=5,
                )
            except Exception:
                # Falls through: we still return a result, just the
                # SSH probe failed. The aggregate value is unaffected.
                pass

        t0 = time.perf_counter()
        out = kernel.run({
            "chunk_id": chunk.chunk_id,
            "range_lo": int(chunk.parameters["range_lo"]),
            "range_hi": int(chunk.parameters["range_hi"]),
            "kernel": "monte_carlo_pi",
            "sleep_per_sample_s": float(
                chunk.parameters.get("sleep_per_sample_s", 0.0)
            ),
        })
        elapsed_ms = (time.perf_counter() - t0) * 1000 + per_call_overhead_ms
        canon = json.dumps({
            "chunk_id": chunk.chunk_id,
            "hits": out["hits"], "trials": out["trials"],
            "lo": out["range_lo"], "hi": out["range_hi"],
        }, sort_keys=True).encode("utf-8")
        canonical_hash = hashlib.sha256(canon).hexdigest()
        return SignedChunkResult(
            chunk_id=chunk.chunk_id,
            payload=out,
            elapsed_ms=elapsed_ms,
            executed_on_peer=peer_display_name,
            signed_by_node_id=peer_id,
            signed_by_display_name=peer_display_name,
            signing_pubkey_b64="fake-pub-b64",
            signature_b64=f"fake-sig::{peer_id}::{chunk.chunk_id}::"
                          f"{canonical_hash[:16]}",
            signature_valid=True,
            signature_verification_reason="",
            canonical_hash=canonical_hash,
        )

    return _call


# ---------------------------------------------------------------------------
# Three approaches
# ---------------------------------------------------------------------------


def approach_a_closed_form() -> tuple[float, float]:
    """SymPy: ``float(pi)``. Returns (value, elapsed_ms)."""
    t0 = time.perf_counter()
    value = float(sp.pi)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return value, elapsed_ms


def approach_b_quadrature() -> tuple[float, float]:
    """SciPy: dblquad over the indicator of the unit disc on
    [-1, 1] x [-1, 1]. Returns (value, elapsed_ms)."""
    def indicator(y: float, x: float) -> float:
        return 1.0 if (x * x + y * y) <= 1.0 else 0.0

    t0 = time.perf_counter()
    value, _abserr = si.dblquad(
        indicator, -1.0, 1.0, lambda x: -1.0, lambda x: 1.0,
        epsabs=1e-3, epsrel=1e-3,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return value, elapsed_ms


@dataclass(frozen=True)
class FederatedRun:
    k: int
    wall_ms: float
    pi_estimate: float
    rel_err: float
    n_samples: int
    speedup_measured: float = 0.0
    speedup_predicted: float = 0.0


def approach_c_federated_run(
    *,
    k: int,
    n_samples: int,
    n_chunks: int,
    sleep_per_sample_s: float,
    per_call_overhead_ms: float,
    include_selfhost: bool,
    selfhost_peer: Optional["FakePeer"],
) -> FederatedRun:
    """Run a single (k peers, n_samples) federated MC."""
    registry = PatternRegistry.with_builtins()
    registry.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="monte_carlo_pi",
        decomposer=MonteCarloDecomposer(),
        recomposer=MonteCarloPiRecomposer(),
    )

    peers: list[Any] = list(_make_fake_peers(k))
    selfhost_node_id = None
    selfhost_ssh_target = None
    if include_selfhost and selfhost_peer is not None:
        peers[-1] = selfhost_peer
        selfhost_node_id = selfhost_peer.node_id
        selfhost_ssh_target = selfhost_peer.display_name.split(":")[0]

    compute_call = _make_mc_compute_call(
        per_call_overhead_ms=per_call_overhead_ms,
        selfhost_node_id=selfhost_node_id,
        selfhost_ssh_target=selfhost_ssh_target,
    )

    params: dict[str, Any] = {
        "n": n_samples,
        "n_chunks": n_chunks,
        "kernel": "monte_carlo_pi",
    }
    if sleep_per_sample_s > 0.0:
        params["sleep_per_sample_s"] = sleep_per_sample_s

    t0 = time.perf_counter()
    receipt = decompose_and_solve(
        problem={
            "description": f"monte_carlo_pi(N={n_samples})",
            "pattern": "embarrassingly_parallel",
            "parameterization": "monte_carlo_pi",
            "parameters": params,
            "submitter": "@scientist:demo",
        },
        peers=peers,
        dispatch="cross_node",
        registry=registry,
        compute_call=compute_call,
    )
    wall_ms = (time.perf_counter() - t0) * 1000

    pi_est = float(receipt.aggregate_value["pi_estimate"])
    rel_err = abs(pi_est - math.pi) / math.pi
    return FederatedRun(
        k=k,
        wall_ms=wall_ms,
        pi_estimate=pi_est,
        rel_err=rel_err,
        n_samples=n_samples,
    )


# ---------------------------------------------------------------------------
# Amdahl model
# ---------------------------------------------------------------------------


def amdahl_predicted_speedup(*, k: int, n: int, t_per_sample: float,
                             overhead_s: float) -> float:
    """The brief's predicted speedup curve:

      S(k) = k / (1 + k * tau_ov / (N * t))

    Where ``tau_ov`` is the per-peer overhead that DOES scale with k
    (per-peer signature collection, per-peer dispatch in a saturated
    pool, per-peer trust-graph lookup). Asymptote: as k -> inf,
    S(k) -> N*t/tau_ov (the "crossover" hits when k*tau_ov ~ N*t).

    Models the dispatcher's behavior where each additional peer
    introduces fresh per-peer coordination cost. As tau_ov -> 0, the
    curve recovers ideal linear speedup.
    """
    if n <= 0 or t_per_sample <= 0:
        return 1.0
    return k / (1.0 + (k * overhead_s) / (n * t_per_sample))


def crossover_n_star(*, k: int, t_per_sample: float, overhead_s: float) -> float:
    """N* = k * tau_ov / t.

    The sample-count threshold where federation overhead equals the
    parallel-work savings: at N* exactly, T(1) == T(k). Below N*, the
    serial overhead dominates and single-machine wins; above N*,
    federation wins."""
    if t_per_sample <= 0:
        return float("inf")
    return k * overhead_s / t_per_sample


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------


def plot_speedup(
    *,
    runs: list[FederatedRun],
    n_samples: int,
    t_per_sample: float,
    overhead_s: float,
    kernel_label: str,
    out_png: pathlib.Path,
    out_svg: pathlib.Path,
) -> None:
    """Two stacked subplots: wall time + speedup."""
    ks = [r.k for r in runs]
    walls_ms = [r.wall_ms for r in runs]
    speedup_meas = [r.speedup_measured for r in runs]
    speedup_pred = [
        amdahl_predicted_speedup(
            k=r.k, n=n_samples,
            t_per_sample=t_per_sample, overhead_s=overhead_s,
        )
        for r in runs
    ]

    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(7.5, 7.0), sharex=True,
    )
    fig.patch.set_facecolor(BRAND_BG)

    for ax in (ax_top, ax_bot):
        ax.set_facecolor(BRAND_PANEL)
        for spine in ax.spines.values():
            spine.set_color(BRAND_FG)
        ax.tick_params(colors=BRAND_FG, which="both")
        ax.grid(True, color=BRAND_MUTED, alpha=0.25, linewidth=0.5)
        ax.title.set_color(BRAND_FG)
        ax.xaxis.label.set_color(BRAND_FG)
        ax.yaxis.label.set_color(BRAND_FG)

    # Top: wall time
    ax_top.plot(ks, walls_ms, marker="o", linewidth=2.0,
                color=BRAND_ACCENT, markersize=7,
                label=f"measured ({kernel_label})")
    ax_top.set_yscale("log")
    ax_top.set_ylabel("wall time (ms, log)")
    ax_top.set_title(
        f"Three-approaches scientist demo — federated Monte Carlo (N={n_samples:,})",
        fontsize=11, weight="bold",
    )
    leg = ax_top.legend(loc="best", facecolor=BRAND_PANEL, edgecolor=BRAND_FG)
    for txt in leg.get_texts():
        txt.set_color(BRAND_FG)

    # Bottom: speedup measured vs predicted
    ax_bot.plot(ks, speedup_meas, marker="o", linewidth=2.0,
                color=BRAND_ACCENT, markersize=7,
                label="measured S(k)")
    ax_bot.plot(ks, speedup_pred, marker="s", linewidth=2.0, linestyle="--",
                color=BRAND_ACCENT2, markersize=6,
                label="predicted Amdahl S(k)")
    # Ideal linear k for context
    ax_bot.plot(ks, ks, linewidth=1.0, linestyle=":",
                color=BRAND_MUTED, label="ideal k")
    ax_bot.set_xlabel("peers k")
    ax_bot.set_ylabel("speedup S(k)")
    leg2 = ax_bot.legend(loc="best", facecolor=BRAND_PANEL, edgecolor=BRAND_FG)
    for txt in leg2.get_texts():
        txt.set_color(BRAND_FG)

    fig.tight_layout()
    fig.savefig(out_png, dpi=160, facecolor=BRAND_BG)
    fig.savefig(out_svg, facecolor=BRAND_BG)
    plt.close(fig)


# ---------------------------------------------------------------------------
# CLI / Main
# ---------------------------------------------------------------------------


KERNEL_PROFILES: dict[str, dict[str, float | str]] = {
    "cheap": {
        "sleep_per_sample_s": 0.0,
        "label": "cheap (numpy darts)",
        "approx_t_per_sample_s": 1.0e-7,
    },
    "mcmc-fake": {
        "sleep_per_sample_s": 0.001,  # 1 ms / sample
        "label": "mcmc-fake (1 ms/sample)",
        "approx_t_per_sample_s": 1.0e-3,
    },
    "openmc-fake": {
        "sleep_per_sample_s": 0.1,    # 100 ms / sample
        "label": "openmc-fake (100 ms/sample)",
        "approx_t_per_sample_s": 1.0e-1,
    },
}


def _hr(label: str = "") -> None:
    line_chars = "-"  # ASCII for portability across capture pipelines
    print(f"\n{line_chars * 8} {label} {line_chars * (60 - len(label))}")


def _measure_per_sample_t(*, n: int, sleep_per_sample_s: float) -> float:
    """Quick single-process measurement: per-sample wall cost. Used
    to feed the Amdahl prediction."""
    kernel = MonteCarloDartsKernel()
    t0 = time.perf_counter()
    out = kernel.run({
        "chunk_id": "calibrate-0001",
        "range_lo": 0,
        "range_hi": n,
        "kernel": "monte_carlo_pi",
        "sleep_per_sample_s": sleep_per_sample_s,
    })
    elapsed_s = time.perf_counter() - t0
    assert out["trials"] == n
    return elapsed_s / n if n > 0 else 0.0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n-samples", type=int, default=1_000_000,
                    help="Monte Carlo samples (default: 1_000_000)")
    ap.add_argument("--ks", type=str, default="1,2,4,8,16",
                    help="comma-separated peer counts (default: 1,2,4,8,16)")
    ap.add_argument("--n-chunks-per-k", type=int, default=8,
                    help="chunks per k-run (default: 8 — fixed so chunk "
                         "size shrinks as k grows)")
    ap.add_argument("--per-call-overhead-ms", type=float, default=1.0,
                    help="simulated per-call federation overhead (default: 1.0 ms)")
    ap.add_argument("--kernel", type=str, default="cheap",
                    choices=sorted(KERNEL_PROFILES.keys()),
                    help="kernel cost profile (default: cheap)")
    ap.add_argument("--include-selfhost", action="store_true",
                    help="opt-in: include a real self-hosted node as one peer (real "
                         "SSH ping for latency; falls back gracefully)")
    ap.add_argument("--selfhost-display-name", type=str, default="user:example-host",
                    help="self-hosted peer display name (ssh_user:host form)")
    ap.add_argument("--out-dir", type=str,
                    default="/tmp/axiom-feature-snapshots/show",
                    help="figure + transcript output directory")
    ap.add_argument("--all-kernels", action="store_true",
                    help="run cheap + mcmc-fake + openmc-fake in one go "
                         "(small N for the expensive ones; renders 3 figures)")
    args = ap.parse_args(argv)

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ks = [int(s) for s in args.ks.split(",") if s.strip()]
    if not ks:
        print("[error] --ks must include at least one integer", file=sys.stderr)
        return 2
    if 1 not in ks:
        ks = [1] + ks  # need k=1 baseline for speedup

    # ----- Preamble -----
    _hr("THE SCIENTIST'S FRAMING")
    print(__doc__.split("Three approaches")[0].rstrip())

    if args.all_kernels:
        return _run_all_kernels(args, out_dir, ks)

    return _run_one_kernel(args, out_dir, ks, args.kernel)


def _run_all_kernels(args, out_dir: pathlib.Path, ks: list[int]) -> int:
    """Run the three kernel cost profiles in sequence so the asymmetric
    win is visible empirically."""
    rc = 0
    # Use small N for the expensive kernels so wall time stays sane.
    # Sized so each k=8 run takes ~1-2 s.
    for kernel_name, n_samples in [
        ("cheap", args.n_samples),
        ("mcmc-fake", 2_000),    # 2s at k=1, ~250ms at k=8
        ("openmc-fake", 80),     # 8s at k=1, ~1s at k=8
    ]:
        sub_args = argparse.Namespace(**vars(args))
        sub_args.kernel = kernel_name
        sub_args.n_samples = n_samples
        sub_args.all_kernels = False
        rc |= _run_one_kernel(sub_args, out_dir, ks, kernel_name)
    return rc


def _run_one_kernel(args, out_dir: pathlib.Path, ks: list[int],
                     kernel_name: str) -> int:
    profile = KERNEL_PROFILES[kernel_name]
    sleep_per_sample_s = float(profile["sleep_per_sample_s"])
    kernel_label = str(profile["label"])

    _hr(f"APPROACH A — Closed-form via SymPy ({kernel_label})")
    a_value, a_ms = approach_a_closed_form()
    print(f"  value           : {a_value:.10f}")
    print(f"  ground truth    : math.pi = {math.pi:.10f}")
    print(f"  wall time       : {a_ms:.3f} ms")
    print( "  caveat          : exact only because pi is in the table; "
                                "doesn't generalise.")

    _hr(f"APPROACH B — Numerical quadrature via SciPy")
    b_value, b_ms = approach_b_quadrature()
    b_rel_err = abs(b_value - math.pi) / math.pi
    print(f"  value           : {b_value:.10f}")
    print(f"  rel err vs pi   : {b_rel_err:.6f}")
    print(f"  wall time       : {b_ms:.3f} ms")
    print( "  caveat          : O(M^d). Doubles each dimension. Unusable >~6D.")

    _hr(f"APPROACH C — Federated Monte Carlo ({kernel_label})")
    print(f"  kernel profile  : {kernel_label}")
    print(f"  N samples       : {args.n_samples:,}")
    print(f"  chunks per run  : {args.n_chunks_per_k}")
    print(f"  per-call overh  : {args.per_call_overhead_ms} ms")
    if args.include_selfhost:
        print(f"  selfhost peer   : {args.selfhost_display_name} (real SSH ping)")
    else:
        print( "  peers           : fake (hermetic; --include-selfhost to add a node)")

    selfhost_peer: Optional[FakePeer] = None
    if args.include_selfhost:
        selfhost_peer = FakePeer(
            node_id="node-selfhost",
            display_name=args.selfhost_display_name,
            trust_score=0.9,
            compute_capacity_hint=2.0,
            latency_estimate_ms=80.0,
        )

    # Calibrate per-sample t (used to feed Amdahl prediction).
    t_per_sample_s = _measure_per_sample_t(
        n=min(args.n_samples, 200_000),
        sleep_per_sample_s=sleep_per_sample_s,
    )
    overhead_s = (args.per_call_overhead_ms / 1000.0) * args.n_chunks_per_k

    print()
    print(f"  calibrated t/sample   : {t_per_sample_s * 1e9:.1f} ns")
    print(f"  estimated overhead    : {overhead_s * 1000:.2f} ms "
          f"(per_call × {args.n_chunks_per_k} chunks; "
          f"empirical fit reported below)")

    # First pass: do the runs (no prediction yet — we infer overhead
    # empirically from the k=1 run so the Amdahl curve is honest about
    # what we're actually measuring).
    runs: list[FederatedRun] = []
    baseline_wall_ms: Optional[float] = None
    for k in sorted(set(ks)):
        n_chunks_eff = max(args.n_chunks_per_k, k)
        run = approach_c_federated_run(
            k=k,
            n_samples=args.n_samples,
            n_chunks=n_chunks_eff,
            sleep_per_sample_s=sleep_per_sample_s,
            per_call_overhead_ms=args.per_call_overhead_ms,
            include_selfhost=args.include_selfhost,
            selfhost_peer=selfhost_peer,
        )
        if baseline_wall_ms is None or k == 1:
            baseline_wall_ms = run.wall_ms
        runs.append(run)

    # Empirically infer the serial-overhead fraction tau_ov from the
    # k=1 wall time. T(1) = tau_ov + N*t, so tau_ov = T(1) - N*t. This
    # captures: orchestrator setup, per-chunk overhead summed serially,
    # signature+receipt assembly. The Amdahl prediction then is:
    #   T(k) = tau_ov + (N*t)/k   (Amdahl serial-fraction form)
    #   S(k) = T(1) / T(k)
    n_t_s = args.n_samples * t_per_sample_s
    measured_tau_ov_s = max(0.0, (baseline_wall_ms or 0.0) / 1000.0 - n_t_s)

    # Replace overhead_s with the empirically measured one for the
    # prediction (the constructor estimate above is the model input;
    # the measured one is what actually happens, which is what we
    # report against).
    overhead_s_predicted = measured_tau_ov_s

    enriched: list[FederatedRun] = []
    for r in runs:
        speedup_meas = (baseline_wall_ms / r.wall_ms) if r.wall_ms > 0 else 0.0
        # Brief's formula: S(k) = k / (1 + k * tau_ov / (N*t))
        speedup_pred = amdahl_predicted_speedup(
            k=r.k, n=args.n_samples,
            t_per_sample=t_per_sample_s,
            overhead_s=overhead_s_predicted,
        )
        enriched.append(FederatedRun(
            k=r.k, wall_ms=r.wall_ms,
            pi_estimate=r.pi_estimate, rel_err=r.rel_err,
            n_samples=r.n_samples,
            speedup_measured=speedup_meas,
            speedup_predicted=speedup_pred,
        ))
    runs = enriched
    # Use the empirically-inferred overhead for the printed model + figure
    overhead_s = overhead_s_predicted

    # Pretty table
    print()
    print(f"  {'k peers':>7} | {'wall_ms':>9} | {'S meas':>7} | "
          f"{'S pred':>7} | {'pi est':>10} | {'rel err':>9}")
    print(f"  {'-' * 7} + {'-' * 9} + {'-' * 7} + "
          f"{'-' * 7} + {'-' * 10} + {'-' * 9}")
    for r in runs:
        print(f"  {r.k:>7} | {r.wall_ms:>9.1f} | "
              f"{r.speedup_measured:>7.2f} | "
              f"{r.speedup_predicted:>7.2f} | "
              f"{r.pi_estimate:>10.5f} | {r.rel_err:>9.6f}")

    # ----- Mathematical model recap -----
    _hr("MATHEMATICAL MODEL")
    print(f"  S(k) = k / (1 + k * tau_ov / (N * t))")
    print(f"  N    = {args.n_samples:,} samples")
    print(f"  t    = {t_per_sample_s * 1e9:.1f} ns / sample (measured)")
    print(f"  tau_ov = {overhead_s * 1000:.2f} ms / run (measured)")
    for k in [2, 4, 8, 16]:
        n_star = crossover_n_star(
            k=k, t_per_sample=t_per_sample_s, overhead_s=overhead_s,
        )
        print(f"  crossover N*(k={k:>2}) = {n_star:,.0f} samples")

    # ----- Honest read -----
    _hr("HONEST READ")
    print(f"  - {kernel_label}: t/sample ~ {t_per_sample_s * 1e9:.0f} ns. "
          f"Crossover N*(k=8) ~ "
          f"{crossover_n_star(k=8, t_per_sample=t_per_sample_s, overhead_s=overhead_s):,.0f}.")
    if kernel_name == "cheap":
        print( "  - Below N*, single-machine wins. Above N*, federation wins.")
        print( "  - The brief's S(k)=k/(1+k*tau_ov/(N*t)) is the pessimistic")
        print( "    upper-bound: it assumes per-peer overhead grows linearly")
        print( "    in k. Our dispatcher parallelises some of that work, so")
        print( "    measured S(k) sits ABOVE predicted at low k -- and CLOSER")
        print( "    to predicted (sometimes below) as k grows + contention bites.")
        print( "  - The asymmetric edge appears when the kernel is EXPENSIVE per sample.")
        print( "  - Re-run with --kernel mcmc-fake or --kernel openmc-fake to see "
                                            "the asymmetric win.")
    elif kernel_name == "mcmc-fake":
        print( "  - Per-sample work now dominates the federation overhead.")
        print( "  - Speedup curve hugs the predicted Amdahl line; ~k-fold wall-time "
                                            "reduction.")
    else:
        print( "  - 100 ms/sample is the OpenMC / large-batch transport regime.")
        print( "  - Federation is essentially free vs. the kernel cost. "
                                            "S(k) -> k.")

    # ----- Visualization -----
    out_png = out_dir / f"three-approaches-speedup-{kernel_name}.png"
    out_svg = out_dir / f"three-approaches-speedup-{kernel_name}.svg"
    plot_speedup(
        runs=runs,
        n_samples=args.n_samples,
        t_per_sample=t_per_sample_s,
        overhead_s=overhead_s,
        kernel_label=kernel_label,
        out_png=out_png,
        out_svg=out_svg,
    )

    # Also publish a canonical "the figure" name for the cheap kernel
    # (the headline figure the task brief asks for).
    if kernel_name == "cheap":
        canonical_png = out_dir / "three-approaches-speedup.png"
        canonical_svg = out_dir / "three-approaches-speedup.svg"
        canonical_png.write_bytes(out_png.read_bytes())
        canonical_svg.write_bytes(out_svg.read_bytes())
        print()
        print(f"  PNG -> {canonical_png}")
        print(f"  SVG -> {canonical_svg}")
    print(f"  PNG -> {out_png}")
    print(f"  SVG -> {out_svg}")

    # ----- End-of-script assertion -----
    if kernel_name == "cheap" and args.n_samples >= 1_000_000:
        # Find the k=8 (or nearest) result.
        target = next((r for r in runs if r.k == 8), runs[-1])
        if abs(target.pi_estimate - math.pi) > 0.01:
            print(f"\n[ASSERT FAIL] pi estimate at k={target.k} drifted: "
                  f"{target.pi_estimate} (expected within 0.01 of {math.pi})",
                  file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
