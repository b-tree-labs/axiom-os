# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The install should not hand a node the defects somebody has to find later.

A companion to ``tests/test_store_health.py``, which detects. This prevents.

Three things measured on a live deployment, all of them decided at install time
and none of them revisited afterwards:

- ``shared_buffers`` was 128 MB, the value Postgres ships. The chart set nothing,
  and Postgres reads the **host's** memory rather than its cgroup, so every
  memory default it chooses is wrong inside a container. That deployment's host
  had 498 GB and its pod was capped at 16Gi.
- the Postgres container declared no ``resources`` at all, which makes the pod
  BestEffort and leaves nothing to size the settings above against.
- ``autovacuum_vacuum_scale_factor`` was stock 0.2, which on an 860-million-row
  table means autovacuum does not fire until 172 million rows are dead.

And one defect that was invisible rather than merely wrong: ``pvc.yaml``
referenced ``.Values.bronze.sizeClass`` while ``values.yaml`` defined
``bronze.storageClass``. Helm resolves an unknown path to nil, the enclosing
``if`` silently never fired, and every install quietly got the cluster's default
storage class instead of the one it asked for. No render fails, no log mentions
it, and the chart looks correct on both sides of the typo.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from axiom.infra.store_health import recommended_postgres_settings  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DATA_PLATFORM_CHART = REPO / "src/axiom/extensions/builtins/data_platform/deploy/helm"
VALUES_REF = re.compile(r"\.Values\.([A-Za-z0-9_.]+)")


def _charts() -> list[Path]:
    return sorted(p.parent for p in (REPO / "src/axiom").rglob("deploy/helm/values.yaml"))


def _size_to_bytes(text: str) -> int:
    """Kubernetes quantity to bytes, enough for the suffixes a chart uses."""
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
             "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    for suffix, mult in units.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * mult)
    return int(float(text))


# ---------------------------------------------------------------------------
# The invisible class: a values path that does not exist
# ---------------------------------------------------------------------------


def test_every_values_path_a_template_reads_exists():
    """Helm resolves an unknown path to nil rather than failing, so a typo in a
    conditional silently disables the feature it guards. This found
    `.Values.bronze.sizeClass` against a `bronze.storageClass` definition — the
    declared storage class had never once been applied."""
    problems: list[str] = []
    for chart in _charts():
        values = yaml.safe_load((chart / "values.yaml").read_text()) or {}
        for template in sorted((chart / "templates").rglob("*.yaml")):
            text = template.read_text()
            for match in VALUES_REF.finditer(text):
                path = match.group(1).rstrip(".")
                # An explicit `| default` makes absence intentional.
                if "| default" in text[match.end(): match.end() + 60]:
                    continue
                node = values
                for part in path.split("."):
                    if isinstance(node, dict) and part in node:
                        node = node[part]
                    else:
                        rel = template.relative_to(REPO)
                        problems.append(f"{rel}: .Values.{path}")
                        break
    assert not problems, "templates read values that do not exist:\n  " + "\n  ".join(
        sorted(set(problems))
    )


def test_the_guard_can_fail(tmp_path):
    """Negative control. A guard that cannot fire is indistinguishable from one
    that passes, and the difference is invisible in a green report."""
    chart = tmp_path / "helm"
    (chart / "templates").mkdir(parents=True)
    (chart / "values.yaml").write_text("bronze:\n  storageClass: \"\"\n")
    (chart / "templates" / "pvc.yaml").write_text("x: {{ .Values.bronze.sizeClass }}\n")
    values = yaml.safe_load((chart / "values.yaml").read_text())
    text = (chart / "templates" / "pvc.yaml").read_text()
    missing = [
        m.group(1) for m in VALUES_REF.finditer(text)
        if m.group(1).split(".")[-1] not in (values.get("bronze") or {})
    ]
    assert missing == ["bronze.sizeClass"]


# ---------------------------------------------------------------------------
# Prevention and detection must not disagree about what correct means
# ---------------------------------------------------------------------------


def test_chart_tuning_matches_what_the_health_check_recommends():
    """The check reports what it expected; the chart ships what it got. If these
    are derived separately they drift, and the report starts being argued with
    rather than acted on."""
    values = yaml.safe_load((DATA_PLATFORM_CHART / "values.yaml").read_text())
    internal = values["database"]["internal"]
    limit = _size_to_bytes(internal["resources"]["limits"]["memory"])
    want = recommended_postgres_settings(limit)
    tuning = internal["tuning"]
    assert tuning["sharedBuffers"] == want["shared_buffers"]
    assert tuning["effectiveCacheSize"] == want["effective_cache_size"]
    assert tuning["maintenanceWorkMem"] == want["maintenance_work_mem"]
    assert tuning["workMem"] == want["work_mem"]
    assert tuning["autovacuumVacuumScaleFactor"] == want["autovacuum_vacuum_scale_factor"]


def test_the_database_container_declares_a_memory_limit():
    """Without one the pod is BestEffort and nothing above can be sized."""
    values = yaml.safe_load((DATA_PLATFORM_CHART / "values.yaml").read_text())
    limits = values["database"]["internal"]["resources"]["limits"]
    assert _size_to_bytes(limits["memory"]) > 0


def test_the_autovacuum_default_is_not_stock():
    """Stock 0.2 is correct for most tables and wrong for the one that matters."""
    values = yaml.safe_load((DATA_PLATFORM_CHART / "values.yaml").read_text())
    factor = float(values["database"]["internal"]["tuning"]["autovacuumVacuumScaleFactor"])
    assert factor < 0.2


# ---------------------------------------------------------------------------
# What the cluster actually receives
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_rendered_postgres_carries_the_settings():
    """Values can be right and never reach the container."""
    out = subprocess.run(
        ["helm", "template", "t", str(DATA_PLATFORM_CHART),
         "--set", "database.internal.password=render-only"],
        capture_output=True, text=True, timeout=180,
    )
    assert out.returncode == 0, out.stderr[-800:]
    pods = [
        doc for doc in yaml.safe_load_all(out.stdout)
        if doc and doc.get("kind") in ("StatefulSet", "Deployment")
    ]
    containers = [
        c for pod in pods
        for c in pod["spec"]["template"]["spec"]["containers"]
        if c["name"] == "postgres"
    ]
    assert containers, "no postgres container rendered"
    container = containers[0]
    args = " ".join(container.get("args", []))
    assert "shared_buffers=" in args, args
    assert "autovacuum_vacuum_scale_factor=" in args, args
    assert container["resources"]["limits"]["memory"], container["resources"]
    # and the value it carries is the sized one, not a stray literal
    limit = _size_to_bytes(container["resources"]["limits"]["memory"])
    assert recommended_postgres_settings(limit)["shared_buffers"] in args


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_a_declared_storage_class_actually_reaches_the_bronze_claim():
    """The bug this file exists for: it rendered nothing at all before."""
    out = subprocess.run(
        ["helm", "template", "t", str(DATA_PLATFORM_CHART),
         "--set", "database.internal.password=render-only",
         "--set", "bronze.storageClass=fast-ssd"],
        capture_output=True, text=True, timeout=180,
    )
    assert out.returncode == 0, out.stderr[-800:]
    claims = [
        doc for doc in yaml.safe_load_all(out.stdout)
        if doc and doc.get("kind") == "PersistentVolumeClaim"
        and doc["metadata"]["name"].endswith("-bronze")
    ]
    assert claims, "no bronze claim rendered"
    assert claims[0]["spec"].get("storageClassName") == "fast-ssd", json.dumps(claims[0]["spec"])
