# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Structural tests for the deploy artifacts (helm chart + terraform module).

The platform rule: K8s Server/Platform-tier extensions ship BOTH
Terraform and Helm. These tests keep the two legs present, parseable,
and generic (no site- or domain-specific values baked in).

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

# Values every render needs (the chart `required`-guards its secrets so
# an install can never silently ship blank credentials).
_BASE_SETS = [
    "langfuse.salt=test-salt",
    "langfuse.nextauthSecret=test-nextauth",
    "langfuse.encryptionKey=test-enc",
    "clickhouse.internal.password=test-ch",
]
_EXTERNAL_PG_SETS = [
    "postgres.external.host=pg.example.internal",
    "postgres.external.database=langfuse",
    "postgres.external.username=langfuse",
    "postgres.external.passwordSecret=pg-credentials",
]
_INTERNAL_PG_SETS = [
    "postgres.mode=internal",
    "postgres.internal.password=test-pg",
]


def _render(extra_sets: list[str]) -> str:
    args = [HELM_BIN, "template", "structtest", str(HELM)]
    for s in _BASE_SETS + extra_sets:
        args += ["--set", s]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"helm template failed:\n{r.stderr}"
    return r.stdout


# ---------------------------------------------------------------------------
# Helm — structure (always runs)
# ---------------------------------------------------------------------------


def test_chart_yaml_parses():
    chart = yaml.safe_load((HELM / "Chart.yaml").read_text())
    assert chart["name"] == "axiom-observability"
    assert chart["apiVersion"] == "v2"
    assert chart["version"]


def test_values_yaml_parses_with_required_keys():
    values = yaml.safe_load((HELM / "values.yaml").read_text())
    # Keys the install skill and the terraform module both set.
    assert "salt" in values["langfuse"]
    assert "nextauthSecret" in values["langfuse"]
    assert "encryptionKey" in values["langfuse"]
    assert values["postgres"]["mode"] in ("external", "internal")
    assert "storage" in values["postgres"]["internal"]
    assert "storageClass" in values["postgres"]["internal"]
    assert "storage" in values["clickhouse"]["internal"]
    assert "storageClass" in values["clickhouse"]["internal"]
    assert "type" in values["service"]
    assert "enabled" in values["ingress"]
    # Planned-sibling seams stay visible (extension ADR-001).
    assert values["prometheus"]["enabled"] is False
    assert values["grafana"]["enabled"] is False


def test_all_templates_exist():
    names = {p.name for p in (HELM / "templates").iterdir()}
    assert {
        "_helpers.tpl",
        "langfuse-web.yaml",
        "langfuse-worker.yaml",
        "postgres.yaml",
        "clickhouse.yaml",
        "secrets.yaml",
        "ingress.yaml",
    }.issubset(names)


# ---------------------------------------------------------------------------
# Helm — render (needs the helm binary)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(HELM_BIN is None, reason="helm binary not on PATH")
def test_chart_renders_external_pg_mode():
    out = _render(_EXTERNAL_PG_SETS)
    kinds = re.findall(r"^kind: (.+)$", out, flags=re.M)
    assert kinds.count("Deployment") == 2  # web + worker
    assert "StatefulSet" in kinds  # clickhouse
    assert "Secret" in kinds
    assert "Service" in kinds
    # External PG mode must NOT bring up a private Postgres.
    assert out.count("kind: StatefulSet") == 1


@pytest.mark.skipif(HELM_BIN is None, reason="helm binary not on PATH")
def test_chart_renders_internal_pg_mode_with_ingress():
    out = _render(_INTERNAL_PG_SETS + ["ingress.enabled=true"])
    kinds = re.findall(r"^kind: (.+)$", out, flags=re.M)
    assert kinds.count("StatefulSet") == 2  # postgres + clickhouse
    assert "Ingress" in kinds


@pytest.mark.skipif(HELM_BIN is None, reason="helm binary not on PATH")
def test_chart_refuses_blank_secrets():
    args = [HELM_BIN, "template", "structtest", str(HELM)]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "required" in r.stderr


# ---------------------------------------------------------------------------
# Terraform — structure (always runs)
# ---------------------------------------------------------------------------

_REQUIRED_VARIABLES = {
    # cluster / chart targeting
    "kubeconfig_path",
    "namespace",
    "release",
    "chart_path",
    # service exposure
    "service_type",
    # postgres tenancy (external = shared OLTP per extension ADR-001)
    "postgres_mode",
    "pg_dsn",
    # secrets — empty string means "mint via random_password"
    "salt",
    "nextauth_secret",
    "encryption_key",
    "postgres_password",
    "clickhouse_password",
    # optional static local-PV pinning (generic; off unless both set)
    "node_name",
    "data_path",
    "clickhouse_storage",
    "postgres_storage",
    # arbitrary chart-value passthrough
    "extra_values",
}

_SENSITIVE_VARIABLES = {
    "pg_dsn",
    "salt",
    "nextauth_secret",
    "encryption_key",
    "postgres_password",
    "clickhouse_password",
}


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
        'resource "kubernetes_secret_v1"',
        'resource "random_password"',
        'resource "helm_release"',
    ):
        assert resource in text, f"main.tf missing {resource}"
    # Static local-PV leg (generic node-pinned shape): storage class +
    # PV exist but only activate when the operator pins a node/path.
    assert 'resource "kubernetes_storage_class_v1"' in text
    assert 'resource "kubernetes_persistent_volume_v1"' in text


def test_terraform_outputs_present():
    text = _tf("outputs.tf")
    for output in ("release_name", "namespace", "langfuse_host_hint",
                   "credentials_secret"):
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
