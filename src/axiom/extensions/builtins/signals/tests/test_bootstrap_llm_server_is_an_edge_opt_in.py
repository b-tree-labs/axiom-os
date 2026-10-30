# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ``llm-server`` pod is emitted only for an Edge node that asked for it.

The generator deployed a bundled llama.cpp pod on every node it bootstrapped,
whatever the node was. ADR-019 makes that pod an Edge-profile component: it
exists so a laptop-class machine has a local model without a serving stack
behind it. On a workstation, server or platform node the real model server
already runs, and the pod is a second, unmanaged one reserving a gigabyte
beside it.

So the manifest carries the Deployment and its Service only when two things
are both true — the node is an Edge profile (``edge``, or ``laptop`` as its
everyday name) *and* the caller opted in. Every other combination emits
nothing, including the one every existing caller is in: no profile given at
all. The bootstrap step itself skips cleanly in that case, running no command,
so a default run is unchanged apart from one line saying why.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

yaml = pytest.importorskip("yaml")

from axiom.extensions.builtins.signals.bootstrap import (  # noqa: E402
    LLM_SERVER_PROFILES,
    NODE_PROFILES,
    Bootstrap,
    BootstrapConfig,
    BootstrapStep,
    StepResult,
)


def _config(**cfg) -> BootstrapConfig:
    return BootstrapConfig(cluster_name="test-cluster", non_interactive=True, **cfg)


def _docs(**cfg) -> list[dict]:
    manifest = Bootstrap(_config(**cfg))._get_llm_manifest()
    return [d for d in yaml.safe_load_all(manifest) if d]


def _kinds(**cfg) -> set[str]:
    return {d["kind"] for d in _docs(**cfg)}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_the_opt_in_profiles_are_edge_and_its_everyday_name():
    """ADR-019 names the profile Edge and its hardware laptop-class."""
    assert sorted(LLM_SERVER_PROFILES) == ["edge", "laptop"]
    assert "edge" in NODE_PROFILES


def test_a_caller_that_never_said_what_the_node_is_gets_no_pod():
    """The existing-caller case: no profile, no opt-in, nothing emitted."""
    assert _config().emits_llm_server is False
    assert _docs() == []


@pytest.mark.parametrize("profile", sorted(LLM_SERVER_PROFILES))
def test_an_edge_node_that_opts_in_gets_the_deployment_and_its_service(profile):
    docs = _docs(node_profile=profile, llm_server_opt_in=True)
    assert {d["kind"] for d in docs} == {"Deployment", "Service"}
    assert all(d["metadata"]["name"] == "llm-server" for d in docs)
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == _config().llm_image


@pytest.mark.parametrize("profile", sorted(LLM_SERVER_PROFILES))
def test_an_edge_node_that_did_not_opt_in_gets_nothing(profile):
    """Edge alone is not enough: the pod costs memory and an image build."""
    assert _config(node_profile=profile).emits_llm_server is False
    assert _docs(node_profile=profile) == []


@pytest.mark.parametrize(
    "profile",
    [p for p in NODE_PROFILES if p not in LLM_SERVER_PROFILES] + ["", "   ", "something-else"],
)
def test_no_other_profile_gets_the_pod_even_when_it_asks(profile):
    """Opting in on a non-Edge node is refused, not honoured. Unknown too."""
    assert _config(node_profile=profile, llm_server_opt_in=True).emits_llm_server is False
    assert _docs(node_profile=profile, llm_server_opt_in=True) == []


def test_the_profile_name_is_read_the_way_adr_019_spells_it():
    """ADR-019's table capitalises the profile; the installer lower-cases it."""
    assert _config(node_profile="Edge", llm_server_opt_in=True).emits_llm_server is True
    assert _config(node_profile=" laptop ", llm_server_opt_in=True).emits_llm_server is True


# ---------------------------------------------------------------------------
# The step
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cfg",
    [
        {},
        {"node_profile": "server", "llm_server_opt_in": True},
        {"node_profile": "edge"},
    ],
    ids=["no-profile", "server-opted-in", "edge-not-opted-in"],
)
def test_the_step_skips_without_running_a_single_command(cfg):
    bootstrap = Bootstrap(_config(**cfg))
    with (
        patch.object(bootstrap, "_run_cmd") as run_cmd,
        patch.object(bootstrap, "check_llm") as check_llm,
    ):
        result = bootstrap.setup_llm()
    assert result.step is BootstrapStep.LLM
    assert result.success and result.skipped
    assert "not deployed" in result.message and "\n" not in result.message
    assert result.details["deployed"] is False
    run_cmd.assert_not_called()
    check_llm.assert_not_called()


def test_the_skip_says_which_half_of_the_gate_was_missing():
    edge_without_opt_in = Bootstrap(_config(node_profile="edge")).setup_llm()
    assert "opted in" in edge_without_opt_in.message

    server_opted_in = Bootstrap(_config(node_profile="server", llm_server_opt_in=True)).setup_llm()
    assert "server" in server_opted_in.message and "Edge" in server_opted_in.message

    unknown = Bootstrap(_config(llm_server_opt_in=True)).setup_llm()
    assert "unspecified" in unknown.message


def test_an_edge_node_that_opted_in_still_reaches_the_deploy_path():
    """The gate lets the real case through: image import, then apply."""
    bootstrap = Bootstrap(_config(node_profile="edge", llm_server_opt_in=True))
    with (
        patch.object(bootstrap, "check_llm") as check_llm,
        patch.object(bootstrap, "_run_cmd") as run_cmd,
        patch("time.sleep"),
    ):
        check_llm.side_effect = [
            {"deployed": False, "running": False},
            {"deployed": True, "running": True},
        ]
        run_cmd.return_value = MagicMock(returncode=0)
        result = bootstrap.setup_llm()

    assert result.success and not result.skipped
    commands = [call.args[0] for call in run_cmd.call_args_list]
    assert any(cmd[:3] == ["k3d", "image", "import"] for cmd in commands)
    assert any(cmd[:2] == ["kubectl", "apply"] for cmd in commands)


def test_a_default_run_carries_on_past_the_skipped_pod():
    """Existing callers run every step; the LLM step now skips instead of deploying."""
    bootstrap = Bootstrap(_config())
    ok = lambda step: StepResult(step=step, success=True, message="OK")  # noqa: E731
    with (
        patch.object(bootstrap, "check_prerequisites", return_value=ok(BootstrapStep.PREREQUISITES)),
        patch.object(bootstrap, "setup_k3d", return_value=ok(BootstrapStep.K3D)),
        patch.object(bootstrap, "setup_postgres", return_value=ok(BootstrapStep.POSTGRES)),
        patch.object(bootstrap, "setup_pgvector", return_value=ok(BootstrapStep.PGVECTOR)),
        patch.object(bootstrap, "run_migrations", return_value=ok(BootstrapStep.MIGRATE)),
        patch.object(bootstrap, "verify_installation", return_value=ok(BootstrapStep.VERIFY)),
        patch.object(bootstrap, "_run_cmd") as run_cmd,
    ):
        results = bootstrap.run()

    assert [r.step for r in results] == list(BootstrapStep)
    llm = next(r for r in results if r.step is BootstrapStep.LLM)
    assert llm.success and llm.skipped
    assert all(r.success for r in results)
    run_cmd.assert_not_called()


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_the_cli_carries_the_profile_and_the_opt_in_into_the_config():
    from axiom.extensions.builtins.signals.bootstrap import main

    with (
        patch("sys.argv", ["bootstrap", "--check", "--node-profile", "edge", "--llm-server"]),
        patch.object(Bootstrap, "__init__", return_value=None) as init,
        patch.object(Bootstrap, "check_only", return_value=[StepResult(BootstrapStep.PREREQUISITES, True, "OK")]),
        pytest.raises(SystemExit),
    ):
        main()

    config = init.call_args[0][0]
    assert config.node_profile == "edge"
    assert config.llm_server_opt_in is True
    assert config.emits_llm_server is True


def test_the_cli_default_is_no_profile_and_no_pod():
    from axiom.extensions.builtins.signals.bootstrap import main

    with (
        patch("sys.argv", ["bootstrap", "--check"]),
        patch.object(Bootstrap, "__init__", return_value=None) as init,
        patch.object(Bootstrap, "check_only", return_value=[StepResult(BootstrapStep.PREREQUISITES, True, "OK")]),
        pytest.raises(SystemExit),
    ):
        main()

    config = init.call_args[0][0]
    assert config.node_profile is None
    assert config.llm_server_opt_in is False
    assert config.emits_llm_server is False
