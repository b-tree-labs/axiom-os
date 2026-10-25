# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Switch a blue/green role to another slot, never leaving it without a backend.

ADR-170 and "never down from our own causes": a deploy brings up the idle slot
with the new release, waits for it to be healthy, sends traffic to both slots,
then to the new one only. The old slot keeps running for rollback, which is the
same switch in the other direction.

Two targets, one sequence:
- Compose: ``docker compose up -d --no-deps`` the idle slot (only that
  container changes), wait for its health check, then rewrite the front's
  upstream file and ``nginx -s reload`` (graceful: in-flight requests finish).
- Kubernetes: ``helm upgrade`` with the caller's values and ``active`` set to
  the current slot (adds or changes the idle slot), wait for its rollout, then
  ``active=both`` and ``active=<to>``. Changing ``active`` changes only Service
  selectors; no pod restarts.

Prints one JSON line per step. Exits non-zero, leaving traffic where it was,
if the idle slot never becomes healthy.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml


def upstream_lines(
    release: str, role: str, port: int, active: str, *, slots: list[str] | None = None
) -> list[str]:
    """nginx ``server`` lines for the slots that should receive traffic.

    ``resolve``: nginx looks the slot's name up again while running (with the
    front's ``resolver`` and a shared ``zone``), so a slot that restarts on a
    new address keeps receiving traffic. Without it nginx resolved the name
    once at start and answered 502 to every sender after a slot restart until
    someone restarted the front by hand.
    """
    targets = list(slots or []) if active == "both" else [active]
    return [f"server {release}-{role}-{s}:{port} resolve;" for s in targets]


def _run(*a: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(a), cwd=cwd, text=True, capture_output=True, check=check)


def _say(step: str, **kw) -> None:
    print(json.dumps({"step": step, **kw}), flush=True)


def _wait(fn, what: str, timeout: float) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return
        time.sleep(3)
    raise SystemExit(
        f"bluegreen: {what} did not happen within {timeout:.0f}s; traffic was not moved"
    )


def switch_compose(
    compose_dir: Path, project: str, role: str, to: str, *, timeout: float = 900, settle: float = 3
) -> None:
    compose = yaml.safe_load((compose_dir / "compose.yaml").read_text())
    front_name, front = next(
        (n, s)
        for n, s in compose["services"].items()
        if (s.get("x-axiom-front") or {}).get("role") == role
    )
    meta = front["x-axiom-front"]
    release, port = meta["release"], meta["port"]
    slot_svc = f"{release}-{role}-{to}"
    if slot_svc not in compose["services"]:
        raise SystemExit(
            f"bluegreen: {slot_svc} is not in compose.yaml; generate it with slot {to!r} first"
        )
    dc = ["docker", "compose", "-p", project, "-f", str(compose_dir / "compose.yaml")]

    _run(*dc, "up", "-d", "--no-deps", slot_svc, cwd=compose_dir)
    _say("started", slot=to)

    def healthy() -> bool:
        cid = _run(*dc, "ps", "-q", slot_svc, cwd=compose_dir, check=False).stdout.strip()
        if not cid:
            return False
        state = _run(
            "docker", "inspect", "-f", "{{.State.Health.Status}}", cid, check=False
        ).stdout.strip()
        return state == "healthy"

    _wait(healthy, f"{slot_svc} becoming healthy", timeout)
    _say("healthy", slot=to)

    def point(active: str) -> None:
        lines = upstream_lines(release, role, port, active, slots=sorted({*meta["slots"], to}))
        body = "\\n".join(lines)
        _run(
            *dc,
            "exec",
            "-T",
            front_name,
            "sh",
            "-c",
            f"printf '{body}\\n' > /front/upstream.conf.new && mv /front/upstream.conf.new /front/upstream.conf "
            f"&& nginx -t -q && nginx -s reload",
            cwd=compose_dir,
        )
        _say("routed", to=active)

    point("both")
    time.sleep(settle)
    point(to)


def switch_k8s(
    release: str,
    chart: str,
    helm_args: list[str],
    role: str,
    to: str,
    *,
    timeout: float = 900,
    settle: float = 3,
) -> None:
    svc = f"{release}-{role}"
    current = (
        _run(
            "kubectl",
            "get",
            "svc",
            svc,
            "-o",
            "jsonpath={.metadata.annotations.axiom\\.io/active}",
            check=False,
        ).stdout.strip()
        or "blue"
    )
    if current == to:
        _say("already-active", slot=to)
        return
    base = ["helm", "upgrade", release, chart, *helm_args]
    _run(*base, "--set", f"roles.{role}.blueGreen.active={current}")
    _run(
        "kubectl",
        "rollout",
        "status",
        f"statefulset/{release}-{role}-{to}",
        f"--timeout={int(timeout)}s",
    )
    _say("healthy", slot=to)

    def endpoints() -> int:
        out = _run(
            "kubectl",
            "get",
            "endpoints",
            svc,
            "-o",
            "jsonpath={.subsets[*].addresses[*].ip}",
            check=False,
        )
        return len(out.stdout.split())

    _run(*base, "--set", f"roles.{role}.blueGreen.active=both")
    _wait(lambda: endpoints() >= 2, "both slots serving", 120)
    _say("routed", to="both")
    time.sleep(settle)
    _run(*base, "--set", f"roles.{role}.blueGreen.active={to}")
    _wait(lambda: endpoints() == 1, f"only {to} serving", 120)
    _say("routed", to=to)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("target", choices=["compose", "k8s"])
    ap.add_argument("--role", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--compose-dir", default=".")
    ap.add_argument("--project", default="node")
    ap.add_argument("--release", default="node")
    ap.add_argument("--chart", default="infra/charts/axiom-node")
    ap.add_argument(
        "--helm-arg",
        action="append",
        default=[],
        help="passed to helm upgrade, e.g. -f values.yaml",
    )
    ap.add_argument("--timeout", type=float, default=900)
    a = ap.parse_args(argv)
    if a.target == "compose":
        switch_compose(Path(a.compose_dir), a.project, a.role, a.to, timeout=a.timeout)
    else:
        args = []
        for h in a.helm_arg:
            args += h.split(" ", 1) if h.startswith("-") and " " in h else [h]
        switch_k8s(a.release, a.chart, args, a.role, a.to, timeout=a.timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
