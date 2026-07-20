# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""A2 — per-principal secret routing + presence pod chart/skill."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from axiom.extensions.builtins.connect.presence import (
    principal_slug,
    secret_ref_for_principal,
)
from axiom.extensions.builtins.connect.skills.presence_deploy import _CHART, presence_deploy
from axiom.extensions.builtins.secrets.providers.protocol import SecretRef


def test_secret_ref_per_principal_round_trips():
    ref = secret_ref_for_principal("@axi:bens", "SLACK_BOT_TOKEN")
    assert ref == "kubernetes://axiom-data/axi-bens/SLACK_BOT_TOKEN"
    parsed = SecretRef.parse(ref)
    assert parsed.scheme == "kubernetes"


def test_principal_slug():
    assert principal_slug("@axi:bens") == "axi-bens"
    assert principal_slug("@axi:local") == "axi-local"


def test_deploy_skill_builds_template_command_by_default():
    res = presence_deploy({"principal": "@axi:bens", "channel": "C123",
                           "accountable_human": "ben@example.com"})
    assert res.ok
    cmd = res.value["command"]
    assert cmd[0:2] == ["helm", "template"]  # dry render by default
    assert "axi-bens" in cmd
    assert "--set" in cmd and "principal=@axi:bens" in cmd
    assert res.value["release"] == "axi-bens"


def test_deploy_skill_requires_principal():
    assert not presence_deploy({}).ok


def test_deploy_skill_executes_with_injected_runner():
    calls = {}

    class _Proc:
        returncode = 0
        stdout = "rendered"
        stderr = ""

    def fake_runner(argv, **kw):
        calls["argv"] = argv
        return _Proc()

    res = presence_deploy({"principal": "@axi:bens", "apply": True, "runner": fake_runner})
    assert res.ok and res.value["returncode"] == 0
    assert calls["argv"][0:2] == ["helm", "upgrade"] and "--install" in calls["argv"]


@pytest.mark.skipif(not shutil.which("helm"), reason="helm not installed")
def test_chart_renders_deployment_sa_and_secret_envs():
    out = subprocess.run(
        ["helm", "template", "axi-bens", str(_CHART),
         "--set", "principal=@axi:bens", "--set", "slug=axi-bens",
         "--set", "channel=C123"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    rendered = out.stdout
    assert "kind: Deployment" in rendered
    assert "kind: ServiceAccount" in rendered
    assert "serviceAccountName: axi-bens" in rendered
    # secrets come from the per-principal Secret, never inline
    assert "secretKeyRef" in rendered and "name: axi-bens" in rendered
    assert "SLACK_BOT_TOKEN" in rendered
