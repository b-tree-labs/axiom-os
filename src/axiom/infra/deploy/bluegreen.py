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


def _pod_diagnosis() -> str:
    """Why a rollout is not ready, captured before the cluster is deleted."""
    parts = []
    for cmd in (
        ["kubectl", "get", "pods", "-o", "wide"],
        ["kubectl", "describe", "pods"],
        ["kubectl", "logs", "-l", "app.kubernetes.io/name=axiom-node", "--all-containers", "--tail=40", "--prefix"],
    ):
        out = subprocess.run(cmd, text=True, capture_output=True, check=False)
        parts.append(f"$ {' '.join(cmd)}\n{out.stdout}{out.stderr}")
    return "\n".join(parts)[-16000:]


def _run(*a: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(list(a), cwd=cwd, text=True, capture_output=True, check=False)
    if check and completed.returncode:
        detail = f"{completed.stdout or ''}{completed.stderr or ''}"
        extra = _pod_diagnosis() if a[:2] == ("kubectl", "rollout") else ""
        raise SystemExit(
            f"command failed ({completed.returncode}): {' '.join(a)}\n{detail[-4000:]}\n{extra}"
        )
    return completed


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


class ComposeSlots:
    """A blue/green role on Compose, as two steps: start the idle slot (only that
    container changes, then wait for its health check), and route the front.
    ``switch_compose`` is the two in order; the smoke-gated switch
    (``axiom.infra.deploy.slot_switch``) runs them as the switch runner's prepare and commit."""

    def __init__(self, compose_dir: Path, project: str, role: str, *, timeout: float = 900) -> None:
        compose = yaml.safe_load((compose_dir / "compose.yaml").read_text())
        self.front_name, front = next(
            (n, s)
            for n, s in compose["services"].items()
            if (s.get("x-axiom-front") or {}).get("role") == role
        )
        self.meta = front["x-axiom-front"]
        self.release, self.port = self.meta["release"], self.meta["port"]
        self.services = compose["services"]
        self.compose_dir, self.project, self.role, self.timeout = compose_dir, project, role, timeout
        self.dc = ["docker", "compose", "-p", project, "-f", str(compose_dir / "compose.yaml")]

    def active(self) -> str:
        """The slot the front sends traffic to now, read from its upstream file ("" when unknown or both)."""
        out = _run(*self.dc, "exec", "-T", self.front_name, "cat", "/front/upstream.conf",
                   cwd=self.compose_dir, check=False).stdout
        found = sorted({s for s in self.meta["slots"] if f"{self.release}-{self.role}-{s}:" in out})
        return found[0] if len(found) == 1 else ""

    def start(self, to: str) -> None:
        slot_svc = f"{self.release}-{self.role}-{to}"
        if slot_svc not in self.services:
            raise SystemExit(
                f"bluegreen: {slot_svc} is not in compose.yaml; generate it with slot {to!r} first"
            )
        _run(*self.dc, "up", "-d", "--no-deps", slot_svc, cwd=self.compose_dir)
        _say("started", slot=to)

        def healthy() -> bool:
            cid = _run(*self.dc, "ps", "-q", slot_svc, cwd=self.compose_dir, check=False).stdout.strip()
            if not cid:
                return False
            state = _run(
                "docker", "inspect", "-f", "{{.State.Health.Status}}", cid, check=False
            ).stdout.strip()
            return state == "healthy"

        _wait(healthy, f"{slot_svc} becoming healthy", self.timeout)
        _say("healthy", slot=to)

    def route(self, active: str, *, slots: list[str] | None = None) -> None:
        lines = upstream_lines(self.release, self.role, self.port, active,
                               slots=sorted({*self.meta["slots"], *(slots or [])}))
        body = "\\n".join(lines)
        _run(
            *self.dc,
            "exec",
            "-T",
            self.front_name,
            "sh",
            "-c",
            f"printf '{body}\\n' > /front/upstream.conf.new && mv /front/upstream.conf.new /front/upstream.conf "
            f"&& nginx -t -q && nginx -s reload",
            cwd=self.compose_dir,
        )
        _say("routed", to=active)


def switch_compose(
    compose_dir: Path, project: str, role: str, to: str, *, timeout: float = 900, settle: float = 3
) -> None:
    slots = ComposeSlots(compose_dir, project, role, timeout=timeout)
    slots.start(to)
    slots.route("both", slots=[to])
    time.sleep(settle)
    slots.route(to, slots=[to])


class K8sSlots:
    """A blue/green role on Kubernetes, as the same two steps: ``helm upgrade`` with
    ``active`` at the current slot (adds or changes the idle one) and wait for its
    rollout; then route by changing ``active`` (Service selectors only, no restarts)."""

    def __init__(self, release: str, chart: str, helm_args: list[str], role: str, *, timeout: float = 900) -> None:
        self.release, self.chart, self.helm_args, self.role, self.timeout = release, chart, helm_args, role, timeout
        self.svc = f"{release}-{role}"

    def active(self) -> str:
        return (
            _run(
                "kubectl",
                "get",
                "svc",
                self.svc,
                "-o",
                "jsonpath={.metadata.annotations.axiom\\.io/active}",
                check=False,
            ).stdout.strip()
            or "blue"
        )

    def _path(self) -> str:
        # A role's values live under roles.<name>, a service's under services.<name>;
        # the front says which (fronts rendered before it existed are roles).
        return (
            _run(
                "kubectl",
                "get",
                "svc",
                self.svc,
                "-o",
                "jsonpath={.metadata.annotations.axiom\\.io/values-path}",
                check=False,
            ).stdout.strip()
            or f"roles.{self.role}"
        )

    def _helm(self, path: str, active: str) -> None:
        _run("helm", "upgrade", self.release, self.chart, *self.helm_args, "--set", f"{path}.blueGreen.active={active}")

    def _endpoints(self) -> int:
        out = _run(
            "kubectl",
            "get",
            "endpoints",
            self.svc,
            "-o",
            "jsonpath={.subsets[*].addresses[*].ip}",
            check=False,
        )
        return len(out.stdout.split())

    def start(self, to: str, *, current: str | None = None) -> None:
        current = current or self.active()
        self._helm(self._path(), current)
        _run(
            "kubectl",
            "rollout",
            "status",
            f"statefulset/{self.release}-{self.role}-{to}",
            f"--timeout={int(self.timeout)}s",
        )
        _say("healthy", slot=to)

    def route(self, active: str, *, slots: list[str] | None = None) -> None:
        self._helm(self._path(), active)
        if active == "both":
            _wait(lambda: self._endpoints() >= 2, "both slots serving", 120)
        else:
            _wait(lambda: self._endpoints() == 1, f"only {active} serving", 120)
        _say("routed", to=active)


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
    slots = K8sSlots(release, chart, helm_args, role, timeout=timeout)
    current = slots.active()
    if current == to:
        _say("already-active", slot=to)
        return
    slots.start(to, current=current)
    slots.route("both")
    time.sleep(settle)
    slots.route(to)


def overlap(
    target: str,
    role: str,
    *,
    release: str = "node",
    compose_dir: Path | None = None,
    project: str = "node",
) -> None:
    """Switch a role that updates by overlap (``overlap.enabled``): its first
    process is the switch supervisor, and SIGHUP starts the new copy beside the
    old one. The container stays up throughout, so the switch never depends on
    a process that exits."""
    if target == "compose":
        _run(
            "docker",
            "compose",
            "-p",
            project,
            "kill",
            "-s",
            "HUP",
            f"{release}-{role}",
            cwd=compose_dir,
        )
    else:
        _run("kubectl", "exec", f"{release}-{role}-0", "--", "sh", "-c", "kill -HUP 1")
    print(json.dumps({"step": "overlap", "role": role}), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("target", choices=["compose", "k8s"])
    ap.add_argument("--role", required=True)
    ap.add_argument("--to", help="the slot to switch to (a blue/green role)")
    ap.add_argument("--overlap", action="store_true", help="a role that updates by overlap")
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
    # LOOP-S5: the smoke-gated switch, on the switch runner. Without --smoke the switch is unchanged.
    ap.add_argument("--smoke", default="", help="a smoke manifest: the role's journeys must pass in the "
                    "idle slot before traffic moves and on the front after (axiom.infra.deploy.slot_switch)")
    ap.add_argument("--idle-url", default="", help="with --smoke: the idle slot's address")
    ap.add_argument("--live-url", default="", help="with --smoke: the front's address")
    ap.add_argument("--soak-s", type=float, default=0.0, help="with --smoke: keep running the journeys this long")
    ap.add_argument("--notify", action="append", default=[], help="with --smoke: who hears the outcome "
                    "(default @site-admin)")
    ap.add_argument("--dry-run", action="store_true", help="with --smoke: say what would run, change nothing")
    ap.add_argument("--commit", default="", help="with --smoke: the commit the release was built from; the "
                    "verdict is kept for it, where the release cut reads it (LOOP-S6)")
    a = ap.parse_args(argv)
    if a.overlap:
        overlap(
            a.target, a.role, release=a.release, compose_dir=Path(a.compose_dir), project=a.project
        )
        return 0
    if not a.to:
        ap.error("--to is required for a blue/green switch")
    if a.smoke:
        from axiom.infra.deploy.slot_switch import run_cli

        return run_cli(a)
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
