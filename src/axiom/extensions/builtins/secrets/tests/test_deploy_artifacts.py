# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Structural tests for the OpenBao deploy artifacts (helm chart + terraform).

The platform rule: K8s Server/Platform-tier extensions ship BOTH
Terraform and Helm. These tests keep the two legs present, parseable,
and generic (no site- or domain-specific values baked in), and they lock
in ADR-003's custody decision: the chart/module never write an unseal key
or root token to cluster storage in sealed mode.

Render tests shell out to the ``helm`` / ``terraform`` binaries when
available and skip otherwise; the parse/structure tests always run.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).parent.parent / "deploy"
HELM = DEPLOY / "helm"
TERRAFORM = DEPLOY / "terraform"

HELM_BIN = shutil.which("helm")
TERRAFORM_BIN = shutil.which("terraform")

_OVERLAYS = ("values-local", "values-selfhosted", "values-enclave")


def _render(overlay: str, extra_sets: list[str] | None = None) -> str:
    args = [HELM_BIN, "template", "sec", str(HELM), "-f", str(HELM / f"{overlay}.yaml")]
    for s in extra_sets or []:
        args += ["--set", s]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"helm template {overlay} failed:\n{r.stderr}"
    return r.stdout


# ---------------------------------------------------------------------------
# Helm — structure (always runs)
# ---------------------------------------------------------------------------


def test_chart_yaml_parses():
    chart = yaml.safe_load((HELM / "Chart.yaml").read_text())
    assert chart["name"] == "axiom-secrets"
    assert chart["apiVersion"] == "v2"
    assert chart["version"]


def test_values_yaml_parses_with_required_keys():
    values = yaml.safe_load((HELM / "values.yaml").read_text())
    # Image split so an air-gapped install can retarget the registry.
    assert "registry" in values["image"]
    assert "repository" in values["image"]
    assert "tag" in values["image"]
    # Server posture + storage + listener + seal seams.
    assert values["server"]["mode"] in ("dev", "sealed")
    assert values["server"]["storage"]["backend"] in ("file", "raft")
    assert "size" in values["server"]["storage"]
    assert "disableMlock" in values["server"]
    assert "tlsDisable" in values["server"]["listener"]
    assert "type" in values["server"]["seal"]
    # Extension wiring (AXIOM_OPENBAO_MOUNT).
    assert "mount" in values["extension"]


def test_all_overlays_parse():
    for overlay in _OVERLAYS:
        yaml.safe_load((HELM / f"{overlay}.yaml").read_text())


def test_all_templates_exist():
    names = {p.name for p in (HELM / "templates").iterdir()}
    assert {
        "_helpers.tpl",
        "statefulset.yaml",
        "service.yaml",
        "configmap.yaml",
        "serviceaccount.yaml",
        "token-secret.yaml",
    }.issubset(names)


# ---------------------------------------------------------------------------
# Helm — render (needs the helm binary)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(HELM_BIN is None, reason="helm binary not on PATH")
@pytest.mark.parametrize("overlay", _OVERLAYS)
def test_every_overlay_renders(overlay):
    out = _render(overlay)
    kinds = re.findall(r"^kind: (.+)$", out, flags=re.M)
    assert "StatefulSet" in kinds
    assert "Service" in kinds
    assert "ServiceAccount" in kinds


@pytest.mark.skipif(HELM_BIN is None, reason="helm binary not on PATH")
def test_dev_mode_is_ephemeral_and_wires_token():
    out = _render("values-local")
    # dev = in-memory: no config ConfigMap, no PVC.
    assert "kind: ConfigMap" not in out
    assert "volumeClaimTemplates" not in out
    assert "-dev" in out
    # dev token Secret exists so AXIOM_OPENBAO_TOKEN can be wired.
    assert "kind: Secret" in out
    assert "-token" in out


@pytest.mark.skipif(HELM_BIN is None, reason="helm binary not on PATH")
@pytest.mark.parametrize("overlay", ("values-selfhosted", "values-enclave"))
def test_sealed_mode_persists_and_holds_no_cleartext_key(overlay):
    out = _render(overlay)
    # Sealed = persistent: HCL ConfigMap + PVC.
    assert "kind: ConfigMap" in out
    assert "volumeClaimTemplates" in out
    assert 'storage "file"' in out
    # ADR-003: the chart writes NO unseal key / root token in sealed mode.
    assert "kind: Secret" not in out


@pytest.mark.skipif(HELM_BIN is None, reason="helm binary not on PATH")
def test_enclave_targets_a_mirror_registry():
    values = yaml.safe_load((HELM / "values-enclave.yaml").read_text())
    # Not docker.io — the enclave pulls from a local mirror.
    assert values["image"]["registry"] != "docker.io"
    out = _render("values-enclave")
    assert f'{values["image"]["registry"]}/openbao/openbao' in out


@pytest.mark.skipif(HELM_BIN is None, reason="helm binary not on PATH")
def test_dev_mode_refuses_blank_root_token():
    args = [
        HELM_BIN, "template", "sec", str(HELM),
        "--set", "server.mode=dev",
        "--set", "server.dev.rootToken=",
    ]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "required" in r.stderr


# ---------------------------------------------------------------------------
# Terraform — structure (always runs)
# ---------------------------------------------------------------------------

_REQUIRED_VARIABLES = {
    "kubeconfig_path",
    "namespace",
    "release",
    "chart_path",
    "values_file",
    "image_registry",
    "image_repository",
    "image_tag",
    "server_mode",
    "storage_size",
    "service_type",
    "mount",
    "dev_root_token",
    "extra_values",
}

_SENSITIVE_VARIABLES = {"dev_root_token"}


def _tf(name: str) -> str:
    return (TERRAFORM / name).read_text()


def test_terraform_module_files_exist():
    for name in ("main.tf", "variables.tf", "outputs.tf", "README.md"):
        assert (TERRAFORM / name).exists(), f"deploy/terraform/{name} missing"


def test_terraform_declares_required_variables():
    declared = set(re.findall(r'variable\s+"([^"]+)"', _tf("variables.tf")))
    missing = _REQUIRED_VARIABLES - declared
    assert not missing, f"variables.tf missing: {sorted(missing)}"


def test_terraform_secret_variables_are_sensitive():
    text = _tf("variables.tf")
    for var in sorted(_SENSITIVE_VARIABLES):
        block = re.search(
            rf'variable\s+"{var}"\s*{{(.*?)^}}', text, flags=re.S | re.M
        )
        assert block, f"variable {var!r} not declared"
        assert "sensitive" in block.group(1), f"variable {var!r} not sensitive"


def test_terraform_core_resources_present():
    text = _tf("main.tf")
    for resource in (
        'resource "kubernetes_namespace_v1"',
        'resource "helm_release"',
    ):
        assert resource in text, f"main.tf missing {resource}"


def test_terraform_mints_no_secrets():
    """ADR-003: OpenBao is custody; the module never mints/holds unseal keys."""
    text = _tf("main.tf")
    assert 'resource "kubernetes_secret_v1"' not in text
    assert 'resource "random_password"' not in text


def test_terraform_outputs_present():
    text = _tf("outputs.tf")
    for output in ("release_name", "namespace", "openbao_url",
                   "openbao_mount", "dev_token_secret"):
        assert f'output "{output}"' in text, f"outputs.tf missing {output}"


def test_deploy_artifacts_are_generic():
    """No site-, host-, or domain-specific strings in deploy artifacts.

    The banned terms are assembled from fragments so this test file
    itself stays clean under the same repo-wide grep it enforces.
    """
    fragments = [
        ("neu", "tron"), ("reac", "tor"), ("nuc", "lear"), ("facil", "it"),
        ("tri", "ga"), ("ne", "tl"), ("ras", "cal"), ("ute", "xas"),
        ("ta", "cc"),
    ]
    banned = re.compile(
        "|".join("".join(pair) for pair in fragments), flags=re.I
    )
    for path in sorted(DEPLOY.rglob("*")):
        if not path.is_file():
            continue
        if ".terraform" in path.parts:
            continue
        hits = banned.findall(path.read_text(errors="replace"))
        assert not hits, f"{path.relative_to(DEPLOY)} contains {sorted(set(hits))}"


# ---------------------------------------------------------------------------
# Terraform — fmt check (needs the terraform binary)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(TERRAFORM_BIN is None, reason="terraform binary not on PATH")
def test_terraform_fmt_clean():
    r = subprocess.run(
        [TERRAFORM_BIN, "fmt", "-check", "-diff", str(TERRAFORM)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, f"terraform fmt -check failed:\n{r.stdout}{r.stderr}"
