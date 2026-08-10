# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""TRIAGE workload-health checks: the class of failure that let the
langfuse ClickHouse pod OOM-crash-loop 7,000+ times unnoticed.

Pure-function tests — no live cluster. The k8s gather step is a thin
adapter exercised separately; classification and remediation planning
are deterministic and tested here.
"""

from __future__ import annotations

from axiom.extensions.builtins.diagnostics.safety import (
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
)
from axiom.extensions.builtins.diagnostics.workload_health import (
    GiB,
    classify_oom_kills,
    classify_workloads,
    plan_oom_remediation,
)


def _pod(**kw):
    """A pod-status record as gathered from the cluster (already parsed)."""
    base = {
        "namespace": "langfuse",
        "name": "langfuse-clickhouse-shard0-0",
        "container": "clickhouse",
        "ready": False,
        "phase": "Running",
        "waiting_reason": None,
        "last_terminated_reason": None,
        "restarts": 0,
        "limit_bytes": None,
        "request_bytes": None,
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# classify_workloads — detection
# --------------------------------------------------------------------------

def test_oomkilled_crashloop_is_critical():
    pods = [
        _pod(
            waiting_reason="CrashLoopBackOff",
            last_terminated_reason="OOMKilled",
            restarts=7059,
            limit_bytes=1536 * 1024**2,
        )
    ]
    findings = classify_workloads(pods)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == SEVERITY_CRITICAL
    assert "OOMKilled" in f.detail
    assert f.metadata["reason"] == "OOMKilled"
    assert f.metadata["restarts"] == 7059
    # ext-prefixed name so it doesn't collide
    assert f.check_name.startswith("diagnostics.")


def test_crashloop_without_oom_is_critical_but_not_oom():
    pods = [_pod(waiting_reason="CrashLoopBackOff", last_terminated_reason="Error", restarts=50)]
    findings = classify_workloads(pods)
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_CRITICAL
    assert findings[0].metadata["reason"] == "Error"


def test_restart_storm_without_crashloop_is_warning():
    # Pod currently Running/ready but has churned many times — early signal.
    pods = [_pod(ready=True, waiting_reason=None, restarts=40, last_terminated_reason="OOMKilled")]
    findings = classify_workloads(pods)
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_WARNING
    assert "restart" in findings[0].title.lower()


def test_healthy_pod_yields_no_finding():
    pods = [_pod(ready=True, waiting_reason=None, restarts=0, last_terminated_reason=None)]
    assert classify_workloads(pods) == []


def test_multiple_pods_each_classified():
    pods = [
        _pod(name="a", ready=True, restarts=0),
        _pod(name="b", waiting_reason="CrashLoopBackOff", last_terminated_reason="OOMKilled", restarts=100),
    ]
    findings = classify_workloads(pods)
    assert {f.metadata["pod"] for f in findings} == {"langfuse/b"}


# --------------------------------------------------------------------------
# plan_oom_remediation — bounded, safe auto-fix proposal
# --------------------------------------------------------------------------

def test_oom_with_low_limit_and_ample_node_memory_proposes_bump():
    pod = _pod(last_terminated_reason="OOMKilled", limit_bytes=1536 * 1024**2)
    plan = plan_oom_remediation(pod, node_free_bytes=400 * GiB)
    assert plan is not None
    # Proposes a bounded bump (default 4x, capped), within node headroom.
    assert plan["new_limit_bytes"] == 6 * GiB  # 1536Mi * 4 rounded up to GiB
    assert plan["reversible"] is True
    assert "kubectl" in plan["command"]
    assert plan["namespace"] == "langfuse"


def test_oom_bump_capped_at_node_headroom_fraction():
    # Tiny node: a 4x bump would exceed the safe fraction of free memory.
    pod = _pod(last_terminated_reason="OOMKilled", limit_bytes=8 * GiB)
    plan = plan_oom_remediation(pod, node_free_bytes=10 * GiB, max_node_fraction=0.5)
    # 4x=32Gi but cap = 50% of 10Gi = 5Gi, which is BELOW current limit ->
    # cannot safely remediate by bumping; must escalate.
    assert plan is None


def test_no_remediation_when_not_oom():
    pod = _pod(last_terminated_reason="Error", limit_bytes=1 * GiB)
    assert plan_oom_remediation(pod, node_free_bytes=400 * GiB) is None


def test_no_remediation_when_no_limit_set():
    # No limit means OOM came from node pressure, not a low cgroup cap —
    # bumping a (nonexistent) limit is the wrong fix; escalate.
    pod = _pod(last_terminated_reason="OOMKilled", limit_bytes=None)
    assert plan_oom_remediation(pod, node_free_bytes=400 * GiB) is None


# --------------------------------------------------------------------------
# classify_oom_kills — generalized to ANY process (host or cgroup)
# --------------------------------------------------------------------------

def test_repeated_oom_of_any_process_is_critical():
    # A non-k8s host process repeatedly OOM-killed must still be caught.
    findings = classify_oom_kills([{"process": "python3", "source": "host", "count": 12}])
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_CRITICAL
    assert findings[0].metadata["process"] == "python3"
    assert findings[0].metadata["count"] == 12


def test_repeated_cgroup_oom_is_critical():
    findings = classify_oom_kills([{"process": "clickhouse-serv", "source": "cgroup", "count": 7059}])
    assert findings[0].severity == SEVERITY_CRITICAL
    assert "clickhouse-serv" in findings[0].title


def test_single_oom_is_warning():
    findings = classify_oom_kills([{"process": "stress-ng", "source": "host", "count": 1}])
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_WARNING


def test_no_oom_events_no_findings():
    assert classify_oom_kills([]) == []
