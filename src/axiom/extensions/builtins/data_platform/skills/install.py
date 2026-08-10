# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.install`` — provision the data-platform on K8s.

Provider-driven by construction: the install skill names no specific
ingest source kind, OLTP database, or vector store. It looks up the
active providers via the registries and asks each one for its Helm
values.

Pure-IaC per ``spec-data-architecture.md``: Terraform → Helm → K3D/K8s
+ pip-install from PyPI.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz
from ..database import default_database_kind_registry
from ..vectorstore import default_vector_store_registry

_CHART_PATH = Path(__file__).parent.parent / "deploy" / "helm"


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    actions: list[str] = []
    errors: list[str] = []

    # ---- 1. preflight capabilities ---------------------------------------
    for binary in ("helm", "kubectl"):
        if shutil.which(binary) is None:
            errors.append(
                f"{binary!r} not found on PATH — install it before `axi data install`"
            )
    if errors:
        return SkillResult(ok=False, errors=errors)

    # ---- 2. detect cluster context --------------------------------------
    ctx_name = params.get("kube_context")
    if not ctx_name:
        r = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return SkillResult(
                ok=False,
                errors=["no active kubectl context; set one or pass --kube-context"],
            )
        ctx_name = r.stdout.strip()
    actions.append(f"target context: {ctx_name}")

    # ---- 3. resolve providers from --db-kind / --vector-kind ------------
    args = params.get("_args_namespace")
    db_kind = params.get("db_kind", "postgres")
    vec_kind = params.get("vector_kind", "pgvector")

    try:
        db_provider = default_database_kind_registry().get(db_kind)
    except KeyError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    try:
        vec_provider = default_vector_store_registry().get(vec_kind)
    except KeyError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    actions.append(f"database kind: {db_kind} ({db_provider.description})")
    actions.append(f"vector-store kind: {vec_kind} ({vec_provider.description})")

    # ---- 4. provider-supplied helm values -------------------------------
    helm_sets: dict[str, str] = {}
    if args is not None:
        helm_sets.update(db_provider.helm_values(args))
        try:
            helm_sets.update(vec_provider.helm_values(args, db_kind=db_kind))
        except ValueError as exc:
            return SkillResult(ok=False, errors=[str(exc)])

    # ---- 5. platform-generic values -------------------------------------
    namespace = params.get("namespace", "axiom-data")
    release = params.get("release", "axiom-data-platform")
    actor = params.get("actor")  # CLI passthrough; falls back to AXIOM_ACTOR
    axiom_version = params.get("axiom_version", "")
    expose = params.get("expose", "ClusterIP")
    node_port = params.get("node_port", 0)
    bronze_size = params.get("bronze_size", "100Gi")

    helm_sets["bronze.size"] = bronze_size
    helm_sets["dagster.webserver.serviceType"] = expose
    if axiom_version:
        helm_sets["dagster.axiomVersion"] = axiom_version
    if node_port:
        helm_sets["dagster.webserver.nodePort"] = str(node_port)

    # ---- 6. provenance rules --------------------------------------------
    rules_path = params.get("rules") or params.get("provenance_rules_file")
    rules_toml = ""
    if rules_path:
        p = Path(rules_path).expanduser()
        if not p.exists():
            return SkillResult(ok=False, errors=[f"provenance rules file not found: {p}"])
        rules_toml = p.read_text()
        actions.append(f"loaded provenance rules from {p}")
    else:
        actions.append(
            "no provenance rules supplied — default = quarantine all "
            "(re-run with --rules <file> to enable ingestion)"
        )

    # ---- 7. assemble helm command ---------------------------------------
    helm_args = [
        "helm", "upgrade", "--install", release, str(_CHART_PATH),
        "--namespace", namespace,
        "--create-namespace",
        "--kube-context", ctx_name,
    ]
    for k, v in helm_sets.items():
        helm_args += ["--set", f"{k}={v}"]
    if rules_toml:
        import tempfile

        tf = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        tf.write(rules_toml)
        tf.close()
        helm_args += ["--set-file", f"provenanceRulesToml={tf.name}"]
    if params.get("dry_run"):
        helm_args.append("--dry-run")
        actions.append("dry-run mode — chart will be rendered but not applied")

    # ---- 8. invoke helm under GUARD ------------------------------------
    actions.append(f"helm upgrade --install {release} (namespace={namespace})")
    with _authz.action(
        verb="install",
        resource=f"data-platform://{namespace}",
        classification=Classification.INTERNAL,
        actor=actor,
    ) as act:
        actions.append(f"audit-receipt: {act.receipt_id}")
        r = subprocess.run(helm_args, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return SkillResult(
                ok=False,
                errors=[f"helm exited {r.returncode}", r.stderr or r.stdout],
                actions_taken=actions,
            )
        actions.append("helm upgrade --install succeeded")

    # ---- 9. delegate to data.diagnose -----------------------------------
    if not params.get("dry_run") and not params.get("skip_diagnose"):
        actions.append("running data.diagnose post-install")
        diag = ctx.registry.invoke(
            "data.diagnose",
            {"namespace": namespace, "release": release, "kube_context": ctx_name},
            ctx,
        )
        actions.extend(diag.actions_taken)
        if not diag.ok:
            return SkillResult(
                ok=False,
                value={"release": release, "namespace": namespace, "diagnose": diag.value},
                actions_taken=actions,
                errors=diag.errors,
            )

    actions.append(
        "platform ready. Register connectors with "
        "`axi data register <name> <kind> ...` (kinds: `axi data list kinds`)."
    )
    return SkillResult(
        ok=True,
        value={"release": release, "namespace": namespace, "context": ctx_name,
               "db_kind": db_kind, "vector_kind": vec_kind},
        actions_taken=actions,
    )
