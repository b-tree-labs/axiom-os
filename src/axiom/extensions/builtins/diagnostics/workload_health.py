# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""TRIAGE workload-health check: container crash-loops & resource exhaustion.

The class of failure this catches is the one that let a langfuse ClickHouse
pod OOM-crash-loop **7,000+ times over 47 days** unnoticed — flooding the
host console with kernel OOM-kills — because its cgroup memory limit
(1.5 GiB) was far below what the workload needed while the node had
hundreds of GiB free.

Two pure functions (tested without a cluster):

- :func:`classify_workloads` — turn parsed pod statuses into Findings
  (CrashLoopBackOff / OOMKilled = critical; restart-storm = warning).
- :func:`plan_oom_remediation` — for the well-understood "OOMKilled because
  the limit is far below usage and the node has ample free memory" case,
  compute a **bounded, reversible** limit bump. Returns ``None`` when a fix
  is not provably safe (no limit set, not enough node headroom, not OOM).

:func:`check_workload_crashloops` is the registered ``safety_check`` entry:
it gathers cluster state, classifies, and — to the greatest extent that is
safe — stages the computed remediation into TRIAGE's ``patches/pending``
review queue with an action record, then surfaces a critical finding so a
human is notified. It applies nothing to the cluster on its own; staging +
a loud finding is the autonomy ceiling for a shared-cluster mutation.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime

from axiom.infra.paths import get_user_state_dir

from .safety import SEVERITY_CRITICAL, SEVERITY_WARNING, Finding

log = logging.getLogger(__name__)

GiB = 1024**3
MiB = 1024**2

# A pod that is healthy now but has restarted this many times is an early
# signal (something is churning it) — warn before it becomes a crash-loop.
RESTART_STORM_THRESHOLD = 20

# Default bounded-bump policy for the OOM auto-remediation proposal.
_DEFAULT_MULTIPLIER = 4
_DEFAULT_MAX_NODE_FRACTION = 0.8


def _pod_id(pod: dict) -> str:
    return f"{pod.get('namespace', '?')}/{pod.get('name', '?')}"


def classify_workloads(pods: Iterable[dict]) -> list[Finding]:
    """Classify parsed pod statuses into Findings.

    Each ``pod`` is a dict with: namespace, name, container, ready (bool),
    waiting_reason, last_terminated_reason, restarts (int), limit_bytes,
    request_bytes. Unknown fields default to safe/absent.
    """
    findings: list[Finding] = []
    for pod in pods:
        waiting = pod.get("waiting_reason")
        last_term = pod.get("last_terminated_reason")
        restarts = int(pod.get("restarts") or 0)
        crashloop = waiting == "CrashLoopBackOff"
        reason = last_term or waiting or "Unknown"

        if crashloop:
            oom = last_term == "OOMKilled"
            findings.append(
                Finding(
                    check_name="diagnostics.workload_crashloop",
                    severity=SEVERITY_CRITICAL,
                    title=f"Pod crash-looping: {_pod_id(pod)} ({reason})",
                    detail=(
                        f"{_pod_id(pod)} container {pod.get('container')!r} is in "
                        f"CrashLoopBackOff after {restarts} restarts; last termination "
                        f"reason {reason!r}."
                        + (
                            " OOMKilled with a memory limit far below usage is almost "
                            "always a too-low cgroup limit." if oom else ""
                        )
                    ),
                    remediation=(
                        "Raise the container memory limit if the node has headroom "
                        "(see staged remediation)." if oom
                        else "Inspect container logs for the crash cause; "
                        "fix config/image, then restart the workload."
                    ),
                    metadata={
                        "pod": _pod_id(pod),
                        "container": pod.get("container"),
                        "reason": reason,
                        "restarts": restarts,
                        "limit_bytes": pod.get("limit_bytes"),
                        "oom": oom,
                    },
                )
            )
        elif restarts >= RESTART_STORM_THRESHOLD:
            findings.append(
                Finding(
                    check_name="diagnostics.workload_restart_storm",
                    severity=SEVERITY_WARNING,
                    title=f"Pod restart storm: {_pod_id(pod)} ({restarts} restarts)",
                    detail=(
                        f"{_pod_id(pod)} has restarted {restarts} times "
                        f"(last reason {reason!r}) though it is currently up — "
                        "an early signal of an unstable workload."
                    ),
                    remediation="Investigate the restart cause before it becomes a crash-loop.",
                    metadata={
                        "pod": _pod_id(pod),
                        "reason": reason,
                        "restarts": restarts,
                        "limit_bytes": pod.get("limit_bytes"),
                    },
                )
            )
    return findings


# A process OOM-killed this many times is recurring (a crash-loop by OOM),
# not a one-off spike — surface it loud.
OOM_REPEAT_CRITICAL = 3


def classify_oom_kills(events: Iterable[dict]) -> list[Finding]:
    """Classify kernel OOM-killer events for **any process** (not just pods).

    The kernel OOM-killer reaps host processes and cgroup-limited containers
    alike. This catches the general class — a runaway process repeatedly
    OOM-killed — regardless of whether it's a Kubernetes pod, a systemd
    service, or a bare process. ``events`` are pre-parsed kernel-log records:
    ``{process, pid, source ('cgroup'|'host'), count}`` aggregated by process.
    """
    findings: list[Finding] = []
    for ev in events:
        proc = ev.get("process") or "unknown"
        count = int(ev.get("count") or 1)
        source = ev.get("source") or "host"
        scope = "cgroup memory limit" if source == "cgroup" else "host memory"
        if count >= OOM_REPEAT_CRITICAL:
            findings.append(
                Finding(
                    check_name="diagnostics.oom_killer",
                    severity=SEVERITY_CRITICAL,
                    title=f"Process repeatedly OOM-killed: {proc} (x{count})",
                    detail=(
                        f"The kernel OOM-killer has reaped {proc!r} {count} times "
                        f"(out of {scope}). Repeated OOM kills mean a too-low limit "
                        f"or a leak — and they flood the kernel log/console."
                    ),
                    remediation=(
                        "Raise the memory limit if the host/node has headroom, or "
                        "fix the leak; for a k8s container see any staged limit bump."
                    ),
                    metadata={"process": proc, "count": count, "source": source},
                )
            )
        else:
            findings.append(
                Finding(
                    check_name="diagnostics.oom_killer",
                    severity=SEVERITY_WARNING,
                    title=f"Process OOM-killed: {proc}",
                    detail=f"{proc!r} was OOM-killed (out of {scope}).",
                    remediation="Watch for recurrence; investigate memory sizing.",
                    metadata={"process": proc, "count": count, "source": source},
                )
            )
    return findings


def _round_up_gib(b: float) -> int:
    return int(math.ceil(b / GiB)) * GiB


def _round_down_gib(b: float) -> int:
    return int(b // GiB) * GiB


def plan_oom_remediation(
    pod: dict,
    *,
    node_free_bytes: int,
    multiplier: int = _DEFAULT_MULTIPLIER,
    max_node_fraction: float = _DEFAULT_MAX_NODE_FRACTION,
) -> dict | None:
    """Propose a bounded, reversible memory-limit bump for an OOMKilled pod.

    Returns a plan dict, or ``None`` when a bump is not provably safe:
    not OOMKilled, no limit set (OOM was node pressure, not a cap), or the
    node lacks headroom to raise the limit above its current value.
    """
    if pod.get("last_terminated_reason") != "OOMKilled":
        return None
    limit = pod.get("limit_bytes")
    if not limit:
        # No cgroup limit -> OOM came from node pressure; bumping a limit
        # that doesn't exist is the wrong fix. Escalate instead.
        return None

    desired = _round_up_gib(limit * multiplier)
    cap = _round_down_gib(node_free_bytes * max_node_fraction)
    new_limit = min(desired, cap)
    if new_limit <= limit:
        # Can't raise the limit within the node's safe headroom — escalate.
        return None

    new_request = max(_round_up_gib(new_limit / 4), GiB)
    ns = pod.get("namespace", "")
    return {
        "namespace": ns,
        "pod": _pod_id(pod),
        "container": pod.get("container"),
        "old_limit_bytes": limit,
        "new_limit_bytes": new_limit,
        "new_request_bytes": new_request,
        "reversible": True,
        "reason": "OOMKilled",
        "rationale": (
            f"OOMKilled at limit {limit / GiB:.2f} GiB; node has "
            f"{node_free_bytes / GiB:.0f} GiB free. Bumping to {new_limit / GiB:.0f} GiB "
            f"(<= {max_node_fraction:.0%} of free) is reversible and within headroom."
        ),
        # Operator applies/reviews this; targets the pod's owning workload.
        "command": (
            f"kubectl -n {ns} patch <statefulset|deployment>/<owner> --type=json -p "
            f'\'[{{"op":"replace",'
            f'"path":"/spec/template/spec/containers/0/resources/limits/memory",'
            f'"value":"{new_limit // GiB}Gi"}}]\''
        ),
    }


# ---------------------------------------------------------------------------
# Cluster adapter + registered check (glue; defensive, never raises)
# ---------------------------------------------------------------------------


def _kubectl_json(args: list[str]) -> dict | None:
    try:
        r = subprocess.run(
            ["kubectl", *args, "-o", "json"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _parse_mem(s: str | None) -> int | None:
    """Parse a k8s memory quantity (e.g. '1536Mi', '2Gi', '512M') to bytes."""
    if not s:
        return None
    units = {"Ki": 1024, "Mi": MiB, "Gi": GiB, "Ti": 1024**4,
             "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    for suf, mult in units.items():
        if s.endswith(suf):
            try:
                return int(float(s[: -len(suf)]) * mult)
            except ValueError:
                return None
    try:
        return int(s)
    except ValueError:
        return None


def gather_pods() -> list[dict]:
    """Best-effort gather of pod statuses across all namespaces. [] if no cluster."""
    blob = _kubectl_json(["get", "pods", "-A"])
    if not blob:
        return []
    pods: list[dict] = []
    for item in blob.get("items", []):
        meta = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})
        containers = spec.get("containers", [{}])
        c0 = containers[0] if containers else {}
        limits = (c0.get("resources", {}) or {}).get("limits", {}) or {}
        requests = (c0.get("resources", {}) or {}).get("requests", {}) or {}
        cs = (status.get("containerStatuses") or [{}])[0]
        waiting = ((cs.get("state") or {}).get("waiting") or {}).get("reason")
        last_term = ((cs.get("lastState") or {}).get("terminated") or {}).get("reason")
        pods.append({
            "namespace": meta.get("namespace"),
            "name": meta.get("name"),
            "container": c0.get("name"),
            "ready": bool(cs.get("ready")),
            "phase": status.get("phase"),
            "waiting_reason": waiting,
            "last_terminated_reason": last_term,
            "restarts": cs.get("restartCount", 0),
            "limit_bytes": _parse_mem(limits.get("memory")),
            "request_bytes": _parse_mem(requests.get("memory")),
        })
    return pods


def _node_free_bytes() -> int | None:
    """Best-effort: total allocatable memory of the largest node (proxy for headroom)."""
    blob = _kubectl_json(["get", "nodes"])
    if not blob:
        return None
    best = 0
    for n in blob.get("items", []):
        alloc = (n.get("status", {}).get("allocatable", {}) or {}).get("memory")
        best = max(best, _parse_mem(alloc) or 0)
    return best or None


def _stage_remediation(plan: dict) -> str | None:
    """Write an action record + proposed patch into TRIAGE's review queue.

    Documents what was found and proposed; a human reviews/applies. Returns
    the written path, or None on failure (never raises).
    """
    try:
        pending = get_user_state_dir() / "agents" / "triage" / "patches" / "pending"
        pending.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe = plan["pod"].replace("/", "_")
        path = pending / f"oom-remediation-{safe}-{stamp}.json"
        record = {
            "kind": "workload_oom_remediation",
            "staged_by": "triage.workload_health",
            "staged_at": datetime.now(UTC).isoformat(),
            "plan": plan,
            "note": (
                "TRIAGE detected an OOMKilled crash-loop and computed a bounded, "
                "reversible memory-limit bump. Review and apply the command, or "
                "set it in the workload's Helm values to survive upgrades."
            ),
        }
        path.write_text(json.dumps(record, indent=2))
        return str(path)
    except Exception as exc:  # noqa: BLE001 — staging must never break the sweep
        log.warning("workload_health: could not stage remediation: %s", exc)
        return None


_OOM_HOST_RE = __import__("re").compile(
    r"Out of memory: Killed process \d+ \(([^)]+)\)"
)
_OOM_CGROUP_RE = __import__("re").compile(
    r"Memory cgroup out of memory: Killed process \d+ \(([^)]+)\)"
)


def gather_oom_events() -> list[dict]:
    """Parse the kernel log for OOM-killer events, aggregated by process.

    Best-effort: tries ``journalctl -k`` then ``dmesg``. [] if neither works.
    """
    text = ""
    for cmd in (["journalctl", "-k", "--no-pager", "-o", "cat"], ["dmesg"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout.strip():
            text = r.stdout
            break
    if not text:
        return []

    counts: dict[tuple[str, str], int] = {}
    for line in text.splitlines():
        m = _OOM_CGROUP_RE.search(line)
        src = "cgroup"
        if not m:
            m = _OOM_HOST_RE.search(line)
            src = "host"
        if not m:
            continue
        key = (m.group(1), src)
        counts[key] = counts.get(key, 0) + 1
    return [
        {"process": proc, "source": src, "count": n}
        for (proc, src), n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def notify_admins(findings: list[Finding]) -> str | None:
    """Post critical findings to the machine's sysadmin Slack channel.

    Recipient comes from ``TRIAGE_SYSADMIN_RECIPIENT`` (a notifications
    recipient/channel handle). Best-effort and deduped per distinct problem,
    so a 7,000-restart flood notifies once — never raises, never spams.
    """
    import os

    recipient = os.environ.get("TRIAGE_SYSADMIN_RECIPIENT")
    critical = [f for f in findings if f.severity == SEVERITY_CRITICAL]
    if not recipient or not critical:
        return None
    try:
        from axiom.governance.classification import Classification

        from ..notifications import NotificationPayload, Priority, SendContext, send

        lines = [f"• {f.title}\n  {f.remediation}" for f in critical[:10]]
        summary = f"TRIAGE: {len(critical)} critical workload finding(s) on this machine"
        body = "\n".join(lines)
        # Dedup on the set of distinct problems so repeated sweeps don't spam.
        dedup = "triage-workload:" + ",".join(sorted(f.check_name + ":" + str(f.metadata.get("pod") or f.metadata.get("process")) for f in critical))
        receipt = send(
            SendContext(),
            actor="@agent:triage",
            recipient=recipient,
            payload=NotificationPayload(summary=summary, body=body),
            classification=Classification.INTERNAL,
            priority=Priority.HIGH,
            intent="triage.workload_health",
            dedup_key=dedup,
        )
        return getattr(receipt, "id", None)
    except Exception as exc:  # noqa: BLE001 — notify must never break the sweep
        log.warning("workload_health: sysadmin notify failed: %s", exc)
        return None


def check_workload_crashloops() -> list[Finding]:
    """Registered safety_check: detect crash-loops/OOM, stage safe fixes.

    Never raises. Returns [] when there is no reachable cluster.
    """
    pods = gather_pods()
    if not pods:
        return []
    findings = classify_workloads(pods)

    node_free = _node_free_bytes()
    by_id = {_pod_id(p): p for p in pods}
    for f in findings:
        if not f.metadata.get("oom") or node_free is None:
            continue
        pod = by_id.get(f.metadata.get("pod"))
        if pod is None:
            continue
        plan = plan_oom_remediation(pod, node_free_bytes=node_free)
        if plan is None:
            continue
        staged = _stage_remediation(plan)
        f.metadata["remediation_plan"] = plan
        if staged:
            f.metadata["staged_patch"] = staged
            f.remediation = (
                f"Staged a reversible memory-limit bump to "
                f"{plan['new_limit_bytes'] // GiB} GiB for review: {staged}"
            )
    notify_admins(findings)
    return findings


def check_oom_kills() -> list[Finding]:
    """Registered safety_check: kernel OOM-killer events for any process.

    Generalizes beyond k8s — catches host processes, systemd services, and
    containers alike. Never raises; [] when the kernel log is unavailable.
    """
    findings = classify_oom_kills(gather_oom_events())
    notify_admins(findings)
    return findings
