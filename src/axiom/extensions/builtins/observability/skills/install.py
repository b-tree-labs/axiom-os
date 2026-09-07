# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``observe.install`` — provision the Langfuse trace+eval substrate on K8s.

Mirrors ``data_platform.install`` shape: preflight binaries → detect
cluster context → assemble helm values → ``helm upgrade --install`` →
optionally invoke ``observe.diagnose`` for post-install health.

Mints required secrets (salt, NextAuth secret, encryption key, DB
passwords) when the caller doesn't supply them, and surfaces the
resolved ``LANGFUSE_HOST`` / ``LANGFUSE_PUBLIC_KEY`` /
``LANGFUSE_SECRET_KEY`` triple in ``SkillResult.value.env`` so the
operator can bind them onto the Axiom process — at which point the
env-driven trace provider picks the Langfuse backend automatically.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

_CHART_PATH = Path(__file__).parent.parent / "deploy" / "helm"


def _mint(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def _resolve_secrets(params: dict[str, Any]) -> dict[str, str]:
    """Return the required secrets, minting any the caller didn't supply.

    In the default external-Postgres mode (per extension ADR-001 / ADR-052),
    Langfuse rides the shared axiom OLTP DB with `schema=langfuse`, so
    we don't mint a postgres_password here — the caller (or the
    `pg_dsn` discovery path) supplies the existing credential. The
    internal-Postgres override path mints one in `_resolve_internal_pg`.
    """
    return {
        "salt": params.get("salt") or _mint(),
        "nextauth_secret": params.get("nextauth_secret") or _mint(),
        "encryption_key": params.get("encryption_key") or _mint(),
        "clickhouse_password": params.get("clickhouse_password") or _mint(16),
    }


def _resolve_pg_dsn(params: dict[str, Any]) -> tuple[str, list[str]]:
    """Return ``(dsn_with_schema, actions)`` for the shared-Postgres path.

    Precedence:
      1. Explicit ``pg_dsn`` param — caller passed it in.
      2. ``DP1_RAG_DSN`` env on the install process — reuses the
         data_platform extension's already-discovered DSN.
      3. Empty string — caller is in internal-Postgres mode and the
         chart will bring up its own Postgres.

    The returned DSN always carries ``?schema=langfuse`` so Prisma's
    migration runner stays out of ``public``.
    """
    import os
    from urllib.parse import urlparse, urlunparse

    actions: list[str] = []
    dsn = params.get("pg_dsn") or os.environ.get("DP1_RAG_DSN", "")
    if not dsn:
        return "", actions

    # Append `?schema=langfuse` (or merge with existing query string).
    p = urlparse(dsn)
    q = p.query
    if "schema=" not in q:
        q = (q + "&" if q else "") + "schema=langfuse"
    dsn = urlunparse(p._replace(query=q))
    actions.append("postgres: reusing shared axiom OLTP with schema=langfuse")
    return dsn, actions


def _ensure_schema_and_extension(pg_dsn: str, actions: list[str]) -> list[str]:
    """One-time `CREATE SCHEMA + CREATE EXTENSION pgcrypto` against shared PG.

    Idempotent; safe to re-run on every install. Returns errors (empty
    list on success). Skipped silently when psycopg2 is unavailable —
    operator will see Prisma fail at first boot, with a clearer error
    than we could produce here.
    """
    try:
        import psycopg2
    except ImportError:
        actions.append("psycopg2 unavailable; skipping schema/extension bootstrap")
        return []
    try:
        with psycopg2.connect(pg_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS langfuse")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            conn.commit()
        actions.append("schema=langfuse + pgcrypto ensured on shared PG")
        return []
    except Exception as exc:  # noqa: BLE001
        return [f"PG schema/extension bootstrap failed: {exc}"]


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    actions: list[str] = []
    errors: list[str] = []

    # ---- 1. preflight binaries ------------------------------------------
    for binary in ("helm", "kubectl"):
        if shutil.which(binary) is None:
            errors.append(
                f"{binary!r} not found on PATH — install it before `axi observe install`"
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

    namespace = params.get("namespace", "axiom-observability")
    release = params.get("release", "axiom-observability")

    # ---- 3. resolve/mint secrets ----------------------------------------
    sec = _resolve_secrets(params)
    minted = [k for k in sec if not params.get(k)]
    if minted:
        actions.append(f"minted secrets: {sorted(minted)}")

    # ---- 4. resolve Postgres path (shared vs. internal) -----------------
    # Default is "external" (shared axiom OLTP, schema=langfuse) per
    # extension ADR-001. Operators wanting full isolation pass postgres_mode=internal
    # and the chart will bring up its own Postgres StatefulSet.
    pg_mode = params.get("postgres_mode", "external")
    pg_dsn = ""
    if pg_mode == "external":
        pg_dsn, pg_actions = _resolve_pg_dsn(params)
        actions.extend(pg_actions)
        if not pg_dsn:
            return SkillResult(
                ok=False,
                errors=[
                    "postgres_mode=external (default) needs a shared PG DSN. "
                    "Pass pg_dsn=... or set DP1_RAG_DSN in env, or pass "
                    "postgres_mode=internal to bring up a private PG."
                ],
                actions_taken=actions,
            )
        schema_errs = _ensure_schema_and_extension(pg_dsn, actions)
        if schema_errs:
            return SkillResult(ok=False, errors=schema_errs, actions_taken=actions)
    else:
        # Internal mode: mint a password for the chart-provisioned PG.
        sec["postgres_password"] = params.get("postgres_password") or _mint(16)
        actions.append("postgres: internal mode — chart will bring up its own Postgres")

    # ---- 5. helm values --------------------------------------------------
    helm_sets: dict[str, str] = {
        "langfuse.salt": sec["salt"],
        "langfuse.nextauthSecret": sec["nextauth_secret"],
        "langfuse.encryptionKey": sec["encryption_key"],
        "postgres.mode": pg_mode,
        "clickhouse.internal.password": sec["clickhouse_password"],
    }
    if pg_mode == "external":
        helm_sets["postgres.external.dsn"] = pg_dsn
    else:
        helm_sets["postgres.internal.password"] = sec["postgres_password"]
    expose = params.get("expose", "ClusterIP")
    helm_sets["service.type"] = expose
    if params.get("node_port"):
        helm_sets["service.nodePort"] = str(params["node_port"])

    # ---- 6. helm command -------------------------------------------------
    helm_args = [
        "helm", "upgrade", "--install", release, str(_CHART_PATH),
        "--namespace", namespace,
        "--create-namespace",
        "--kube-context", ctx_name,
    ]
    for k, v in helm_sets.items():
        helm_args += ["--set", f"{k}={v}"]
    if params.get("dry_run"):
        helm_args.append("--dry-run")
        actions.append("dry-run mode — chart will be rendered but not applied")

    actions.append(f"helm upgrade --install {release} (namespace={namespace})")
    r = subprocess.run(helm_args, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return SkillResult(
            ok=False,
            errors=[f"helm exited {r.returncode}", r.stderr or r.stdout],
            actions_taken=actions,
        )
    actions.append("helm upgrade --install succeeded")

    # ---- 7. derive LANGFUSE_* env for the operator -----------------------
    # The chart exposes the web pod on ClusterIP by default; the
    # operator binds these LANGFUSE_* vars onto the Axiom process to
    # turn on the env-driven Langfuse trace provider.
    host = params.get("host_override") or f"http://{release}-web.{namespace}.svc:3000"
    public_key = params.get("public_key", "")  # surfaced by the bootstrap project once available
    secret_key = params.get("secret_key", "")

    env = {
        "LANGFUSE_HOST": host,
        "LANGFUSE_PUBLIC_KEY": public_key,
        "LANGFUSE_SECRET_KEY": secret_key,
    }

    # ---- 8. delegate to observe.diagnose --------------------------------
    if not params.get("dry_run") and not params.get("skip_diagnose"):
        actions.append("running observe.diagnose post-install")
        try:
            diag = ctx.registry.invoke(
                "observe.diagnose",
                {"namespace": namespace, "release": release, "kube_context": ctx_name},
                ctx,
            )
            actions.extend(diag.actions_taken)
            if not diag.ok:
                return SkillResult(
                    ok=False,
                    value={
                        "release": release, "namespace": namespace,
                        "context": ctx_name, "env": env, "secrets": sec,
                        "diagnose": diag.value,
                    },
                    actions_taken=actions,
                    errors=diag.errors,
                )
        except KeyError:
            actions.append("observe.diagnose not registered; skipping post-install probe")

    actions.append(
        "Langfuse install complete. Bind LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY "
        "on the Axiom process to enable trace export."
    )
    return SkillResult(
        ok=True,
        value={
            "release": release,
            "namespace": namespace,
            "context": ctx_name,
            "env": env,
            "secrets": sec,
        },
        actions_taken=actions,
    )
