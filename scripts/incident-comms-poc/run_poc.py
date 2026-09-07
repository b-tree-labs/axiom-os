#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Live proof of the Human<>Agent comms scenario on real Slack, resolving a
REAL config change on a self-hosted node — built on the standard primitives:
  - axiom.infra.host_exec        (run kubectl locally or over SSH, uniformly)
  - diagnostics.incident_interview (the agent's "right questions" + answers)
  - diagnostics.incident_comms   (the HITL conversation)
  - notifications…slack_interactive (the Slack channel provider)

Env (poc/.env): SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_CHANNEL.
Real fix against a remote cluster (example):
  AXI_POC_APPLY=kubectl AXI_POC_KCTL_SSH=my-cluster-host AXI_POC_KUBECONFIG=$HOME/.kube/config
  AXI_POC_KCTL_NS=langfuse AXI_POC_KCTL_STS=langfuse-clickhouse-shard0 AXI_POC_NEW_LIMIT_GIB=24
--local runs a zero-credential transcript proof.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from axiom.extensions.builtins.diagnostics import incident_interview  # noqa: E402
from axiom.extensions.builtins.diagnostics.incident_comms import IncidentConversation  # noqa: E402
from axiom.extensions.builtins.diagnostics.safety import SEVERITY_CRITICAL, Finding  # noqa: E402
from axiom.extensions.builtins.notifications.channels.slack_interactive import (  # noqa: E402
    SlackInteractiveChannel,
)
from axiom.infra.host_exec import HostTarget, executor_for  # noqa: E402

_GiB = 1024**3
_NS = os.environ.get("AXI_POC_KCTL_NS", "langfuse")
_STS = os.environ.get("AXI_POC_KCTL_STS", "langfuse-clickhouse-shard0")
_NEW_GIB = int(os.environ.get("AXI_POC_NEW_LIMIT_GIB", "24"))
_REAL = os.environ.get("AXI_POC_APPLY") == "kubectl"


def _executor():
    ssh = os.environ.get("AXI_POC_KCTL_SSH")
    if ssh:
        return executor_for(HostTarget(name=ssh, kind="ssh", ssh_host=ssh,
                                       env={"KUBECONFIG": os.environ.get("AXI_POC_KUBECONFIG", os.path.expanduser("~/.kube/config"))}))
    return executor_for(HostTarget(name="local", kind="local"))


_EX = _executor()


def _live_limit_mib() -> int | None:
    r = _EX.run(["kubectl", "-n", _NS, "get", f"statefulset/{_STS}",
                 "-o", "jsonpath={.spec.template.spec.containers[0].resources.limits.memory}"])
    out = (r.stdout or "").strip()
    if not r.ok or not out:
        return None
    try:
        if out.endswith("Gi"):
            return int(float(out[:-2]) * 1024)
        if out.endswith("Mi"):
            return int(float(out[:-2]))
    except ValueError:
        return None
    return None


def _live_answer() -> str | None:
    if not _REAL:
        return None
    mib = _live_limit_mib()
    return f"Live current limit on {_STS}: {mib} MiB. Proposed: {_NEW_GIB} GiB." if mib is not None else None


def simulated_oom_finding() -> Finding:
    old_mib = _live_limit_mib() if _REAL else 1536
    return Finding(
        check_name="diagnostics.workload_crashloop",
        severity=SEVERITY_CRITICAL,
        title=f"Pod crash-looping: {_NS}/{_STS}-0 (OOMKilled)",
        detail="CrashLoopBackOff (OOMKilled) — ClickHouse memory limit too low for its working set. "
        "A reversible cgroup-limit bump fixes it.",
        remediation=f"Staged a reversible memory-limit bump → {_NEW_GIB} GiB.",
        metadata={
            "pod": f"{_NS}/{_STS}-0", "reason": "OOMKilled", "restarts": 7059, "oom": True,
            "remediation_plan": {
                "namespace": _NS, "statefulset": _STS,
                "old_limit_bytes": (old_mib or 1536) * 1024**2,
                "new_limit_bytes": _NEW_GIB * _GiB, "reversible": True,
            },
        },
    )


def remediator(plan: dict) -> dict:
    new_gib = plan.get("new_limit_bytes", 0) // _GiB
    if not _REAL:
        return {"ok": True, "verified": True, "new_limit_gib": new_gib, "mode": "demo"}
    patch = ('[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory",'
             f'"value":"{new_gib}Gi"}}]')
    r = _EX.run(["kubectl", "-n", _NS, "patch", f"statefulset/{_STS}", "--type=json", "-p", patch])
    verified = r.ok and _live_limit_mib() == new_gib * 1024
    return {"ok": r.ok, "verified": verified, "new_limit_gib": new_gib,
            "mode": "kubectl(ssh)" if os.environ.get("AXI_POC_KCTL_SSH") else "kubectl",
            "detail": r.output[:200]}


def _conversation(channel):
    # Talk-about-anything: route free text through AXI (the chat agent), with
    # the live-bound interview as the structured fallback when no LLM is
    # configured. Set AXI_POC_INTERVIEW_ONLY=1 to force the canned interview.
    interview = incident_interview.make_responder(live=_live_answer)
    if os.environ.get("AXI_POC_INTERVIEW_ONLY"):
        responder = interview
    else:
        from axiom.extensions.builtins.diagnostics.incident_chat import make_axi_responder
        responder = make_axi_responder(fallback=interview)
    # Agent identity (AgentCard): name + optional avatar. Set AXI_POC_AGENT_ICON
    # to a public image URL to give AXI a face in the channel.
    return IncidentConversation(
        channel, responder=responder, remediator=remediator,
        agent=os.environ.get("AXI_POC_AGENT", "AXI"),
        agent_icon=os.environ.get("AXI_POC_AGENT_ICON") or None,
    )


def _run_local() -> int:
    from axiom.extensions.builtins.notifications.channels.interactive import InMemoryInteractiveChannel

    ch = InMemoryInteractiveChannel()
    conv = _conversation(ch)
    conv.open(simulated_oom_finding())
    ch.inject_message("what is the current limit and is it reversible?")
    ch.inject_action("approve", actor="@ben")
    print("\n──────── #channel (in-memory proof) ────────")
    for p in ch.posts:
        who = "TRIAGE/TIDY" if p.author == "agent" else p.author
        print(f"\n[{who}]{' [buttons]' if p.kind == 'approval' else ''}\n{p.text}")
    print(f"\n──────── status: {conv.status} ────────")
    return 0 if conv.status == "applied" else 1


def main() -> int:
    if "--local" in sys.argv:
        return _run_local()
    for var in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL"):
        if not os.environ.get(var):
            print(f"ERROR: {var} not set (see poc/README.md, or use --local)", file=sys.stderr)
            return 1
    channel = SlackInteractiveChannel(
        bot_token=os.environ["SLACK_BOT_TOKEN"], app_token=os.environ["SLACK_APP_TOKEN"],
        channel=os.environ["SLACK_CHANNEL"],
    )
    conv = _conversation(channel)
    conv.open(simulated_oom_finding())
    print(f"Posted incident to Slack ({'REAL on '+os.environ.get('AXI_POC_KCTL_SSH','local') if _REAL else 'demo'}). Ctrl-C to stop.")
    channel.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
