# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The cluster base on a real cluster: install it, check the contract, use it.

ADR-215 layer 2 at the small size, on a K3D cluster this script creates and
deletes (and nothing else). It never changes the default kubeconfig: the
cluster's kubeconfig is a file in a temporary directory.

1. Install ``infra/charts/axiom-cluster`` with ``values-small.yaml`` and wait.
2. ``python -m axiom.infra.deploy.cluster_contract`` must pass.
3. Use each name the way the application will: a PostgreSQL ``Cluster`` on
   ``axiom-fast`` becomes ready and answers a query; a Secret written to the store's namespace reaches another namespace
   through an ``ExternalSecret`` on the store ``axiom``.

Usage: cluster_base_smoke.py [--keep]   (needs docker, k3d, helm, kubectl)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHART = HERE.parent / "charts" / "axiom-cluster"
NAME = "axiom-cluster-smoke"
#: The operator's own PostgreSQL image. It proves the operator and the storage
#: class, not the extensions: an image with TimescaleDB and pgvector built for
#: CloudNativePG is follow-up work (ADR-215), until then the medallion role
#: keeps its own database image.
PG_IMAGE = "ghcr.io/cloudnative-pg/postgresql:16"


def sh(
    *a: str, env: dict | None = None, check: bool = True, timeout: int = 900
) -> subprocess.CompletedProcess:
    r = subprocess.run(list(a), text=True, capture_output=True, env=env, timeout=timeout)
    if check and r.returncode:
        raise SystemExit(
            f"`{' '.join(a[:4])}` failed ({r.returncode}):\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
        )
    return r


def wait_for(what: str, probe, timeout_s: int = 600, every_s: float = 5) -> None:
    end = time.time() + timeout_s
    while time.time() < end:
        if probe():
            print(f"ok: {what}")
            return
        time.sleep(every_s)
    raise SystemExit(f"timed out after {timeout_s}s waiting for {what}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--keep", action="store_true", help="leave the cluster running for inspection")
    args = ap.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="axiom-cluster-smoke-"))
    env = {**os.environ, "KUBECONFIG": str(work / "kubeconfig")}
    k = lambda *a, **kw: sh("kubectl", *a, env=env, **kw)  # noqa: E731

    sh(
        "k3d",
        "cluster",
        "create",
        NAME,
        "--wait",
        "--no-lb",
        "--servers",
        "1",
        "--agents",
        "0",
        "--kubeconfig-update-default=false",
        "--kubeconfig-switch-context=false",
    )
    try:
        (work / "kubeconfig").write_text(sh("k3d", "kubeconfig", "get", NAME).stdout)

        # 1. The cluster base, at the small size.
        sh("helm", "dependency", "build", str(CHART), env=env)
        sh(
            "helm",
            "install",
            "base",
            str(CHART),
            "-n",
            "axiom-system",
            "--create-namespace",
            "-f",
            str(CHART / "values-small.yaml"),
            "--wait",
            "--timeout",
            "12m",
            env=env,
            timeout=900,
        )
        print("ok: cluster base installed")

        # 2. The contract.
        contract = sh(
            sys.executable, "-m", "axiom.infra.deploy.cluster_contract", env=env, check=False
        )
        print(contract.stdout.rstrip())
        if contract.returncode:
            raise SystemExit("the cluster does not meet the contract")

        # 3a. Postgres on axiom-fast, through the operator.
        db = {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Cluster",
            "metadata": {"name": "axiom-db", "namespace": "default"},
            "spec": {
                "instances": 1,
                "imageName": PG_IMAGE,
                "storage": {"storageClass": "axiom-fast", "size": "1Gi"},
                "resources": {
                    "requests": {"cpu": "100m", "memory": "256Mi"},
                    "limits": {"memory": "512Mi"},
                },
            },
        }
        subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=json.dumps(db),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )

        def db_ready() -> bool:
            r = k("get", "cluster.postgresql.cnpg.io", "axiom-db", "-o", "json", check=False)
            if r.returncode:
                return False
            return json.loads(r.stdout).get("status", {}).get("readyInstances", 0) >= 1

        wait_for("Postgres cluster axiom-db ready on axiom-fast", db_ready, timeout_s=600)
        pvc = json.loads(k("get", "pvc", "-l", "cnpg.io/cluster=axiom-db", "-o", "json").stdout)
        classes = {p["spec"].get("storageClassName") for p in pvc["items"]}
        if classes != {"axiom-fast"}:
            raise SystemExit(f"the database's volume is on {classes}, not axiom-fast")
        q = k("exec", "axiom-db-1", "-c", "postgres", "--", "psql", "-tAc", "select 1")
        if q.stdout.strip() != "1":
            raise SystemExit(f"the database did not answer: {q.stdout}{q.stderr}")
        print("ok: the database answers, its volume is on axiom-fast")

        # 3b. A secret through the store axiom.
        k(
            "create",
            "secret",
            "generic",
            "smoke-credential",
            "-n",
            "axiom-secrets",
            "--from-literal=password=smoke-not-a-secret",
        )
        es = {
            "apiVersion": "external-secrets.io/v1",
            "kind": "ExternalSecret",
            "metadata": {"name": "smoke-credential", "namespace": "default"},
            "spec": {
                "refreshInterval": "10s",
                "secretStoreRef": {"kind": "ClusterSecretStore", "name": "axiom"},
                "target": {"name": "smoke-credential"},
                "data": [
                    {
                        "secretKey": "password",
                        "remoteRef": {"key": "smoke-credential", "property": "password"},
                    }
                ],
            },
        }
        subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=json.dumps(es),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )

        def synced() -> bool:
            r = k("get", "secret", "smoke-credential", "-n", "default", "-o", "json", check=False)
            if r.returncode:
                return False
            value = json.loads(r.stdout).get("data", {}).get("password", "")
            return base64.b64decode(value).decode() == "smoke-not-a-secret"

        wait_for(
            "a secret reached another namespace through the store axiom", synced, timeout_s=180
        )
        print("PASS: the small cluster base meets the contract and each name works")
        return 0
    except BaseException:
        for cmd in (
            ["get", "pods", "-A", "-o", "wide"],
            ["get", "events", "-A", "--sort-by=.lastTimestamp"],
        ):
            print(
                f"$ kubectl {' '.join(cmd)}\n{k(*cmd, check=False).stdout[-4000:]}", file=sys.stderr
            )
        raise
    finally:
        if not args.keep:
            sh("k3d", "cluster", "delete", NAME, check=False)


if __name__ == "__main__":
    sys.exit(main())
