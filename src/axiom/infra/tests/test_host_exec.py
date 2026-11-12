# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Standard host-exec capability (ADR-074): run a command locally or on a
remote host over SSH, uniformly and governed — the shared primitive any
connector/remediation uses when it must act on a host ("if ssh is needed")."""

from __future__ import annotations

from axiom.infra.host_exec import (
    ExecResult,
    HostTarget,
    LocalExecutor,
    SshExecutor,
    build_ssh_argv,
    executor_for,
)


def test_build_ssh_argv_quotes_args_and_passes_env():
    argv = ["kubectl", "patch", "sts/x", "--type=json", "-p", '[{"op":"replace"}]']
    out = build_ssh_argv("example-host", argv, {"KUBECONFIG": "~/.kube/config-example-host"})
    assert out[0] == "ssh" and out[1] == "example-host"
    remote = out[2]
    assert remote.startswith("KUBECONFIG=")          # env prefixed
    assert "kubectl patch sts/x" in remote
    assert '[{"op":"replace"}]' in remote             # json survives, quoted


def test_executor_for_routes_local_vs_ssh():
    assert isinstance(executor_for(HostTarget(name="here", kind="local")), LocalExecutor)
    ex = executor_for(HostTarget(name="example-host", kind="ssh", ssh_host="example-host"))
    assert isinstance(ex, SshExecutor)


def test_local_executor_runs_a_command():
    r = LocalExecutor().run(["printf", "hello"])
    assert isinstance(r, ExecResult)
    assert r.ok and r.stdout == "hello"


def test_ssh_executor_builds_but_is_not_invoked_here():
    # We don't reach the network in unit tests — just assert the command shape.
    ex = SshExecutor(host="example-host", env={"KUBECONFIG": "~/.kube/config-example-host"})
    argv = ex._argv(["kubectl", "get", "pods"])
    assert argv[:2] == ["ssh", "example-host"]
    assert "kubectl get pods" in argv[2]


def test_exec_result_ok():
    assert ExecResult(rc=0, stdout="x", stderr="").ok is True
    assert ExecResult(rc=1, stdout="", stderr="boom").ok is False
