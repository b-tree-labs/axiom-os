# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Check a cluster against the contract the application chart relies on.

ADR-215: every size above micro sees the same names, whether a cloud manages
what is behind them or ``infra/charts/axiom-cluster`` installed it. This reads
the cluster the current kubeconfig points at and says, for each name, whether
it is there and ready:

- storage classes ``axiom-fast`` and ``axiom-standard``;
- ingress class ``axiom``, and a running controller for it;
- a ready certificate issuer, ``axiom-letsencrypt`` or ``axiom-selfsigned``;
- the CloudNativePG operator (its ``Cluster`` resource and a running operator);
- a ready secret store ``axiom``.

Read-only. Prints one line per check (``--json`` for one JSON object per line)
and exits non-zero if any check fails, so it can gate a deploy.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass

#: Runs ``kubectl`` with the given arguments and returns its parsed JSON
#: output, or None when the object does not exist.
Kubectl = Callable[[list[str]], dict | None]

ISSUERS = ("axiom-letsencrypt", "axiom-selfsigned")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def kubectl(args: list[str]) -> dict | None:
    out = subprocess.run(
        ["kubectl", *args, "-o", "json"], text=True, capture_output=True, check=False
    )
    if out.returncode != 0:
        if "NotFound" in out.stderr or "the server doesn't have a resource type" in out.stderr:
            return None
        raise RuntimeError(f"kubectl {' '.join(args)}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def _ready(obj: dict | None) -> bool:
    for c in (obj or {}).get("status", {}).get("conditions", []):
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


def _available(k: Kubectl, selector: str) -> int:
    found = k(["get", "deployments", "--all-namespaces", "-l", selector]) or {}
    return sum(
        int(d.get("status", {}).get("availableReplicas") or 0) for d in found.get("items", [])
    )


def check_storage_class(k: Kubectl, name: str) -> Check:
    sc = k(["get", "storageclass", name])
    if sc is None:
        return Check(f"storage class {name}", False, "missing")
    return Check(f"storage class {name}", True, sc.get("provisioner", ""))


def check_ingress_class(k: Kubectl) -> Check:
    ic = k(["get", "ingressclass", "axiom"])
    if ic is None:
        return Check("ingress class axiom", False, "missing")
    controller = ic.get("spec", {}).get("controller", "")
    if controller == "traefik.io/ingress-controller":
        n = _available(k, "app.kubernetes.io/name=traefik")
        if n < 1:
            return Check(
                "ingress class axiom", False, f"{controller}: no Traefik replica available"
            )
        return Check("ingress class axiom", True, f"{controller}, {n} available")
    # Another controller: the class is bound; whether it serves is the platform's.
    return Check("ingress class axiom", True, controller)


def check_issuer(k: Kubectl) -> Check:
    for name in ISSUERS:
        issuer = k(["get", "clusterissuer", name])
        if issuer is not None:
            return Check(
                "certificate issuer",
                _ready(issuer),
                name if _ready(issuer) else f"{name} not ready",
            )
    return Check("certificate issuer", False, f"none of {', '.join(ISSUERS)}")


def check_postgres_operator(k: Kubectl) -> Check:
    if k(["get", "crd", "clusters.postgresql.cnpg.io"]) is None:
        return Check("postgres operator", False, "CloudNativePG Cluster resource missing")
    n = _available(k, "app.kubernetes.io/name=cloudnative-pg")
    return Check("postgres operator", n >= 1, f"CloudNativePG, {n} available")


def check_secret_store(k: Kubectl) -> Check:
    store = k(["get", "clustersecretstore", "axiom"])
    if store is None:
        return Check("secret store axiom", False, "missing")
    if not _ready(store):
        return Check("secret store axiom", False, "not ready")
    provider = next(iter(store.get("spec", {}).get("provider", {}) or {}), "")
    return Check("secret store axiom", True, provider)


def run(k: Kubectl = kubectl) -> list[Check]:
    return [
        check_storage_class(k, "axiom-fast"),
        check_storage_class(k, "axiom-standard"),
        check_ingress_class(k),
        check_issuer(k),
        check_postgres_operator(k),
        check_secret_store(k),
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--json", action="store_true", help="one JSON object per check")
    args = ap.parse_args(argv)
    checks = run()
    for c in checks:
        if args.json:
            print(json.dumps(asdict(c)))
        else:
            print(f"{'ok  ' if c.ok else 'FAIL'}  {c.name}: {c.detail}")
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
