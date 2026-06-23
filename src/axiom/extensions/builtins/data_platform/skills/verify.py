# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi data verify`` — pre-flight health check for the data platform.

Tonight's stand-up exposed ~10 failure patterns that all looked like
operator-facing SUCCESS until something silently broke downstream:
missing Python deps, missing OCR binaries, expired Box tokens, missing
schema, lost PYTHONPATH after helm rollout, etc.

``verify`` catches them ALL at the place operators ask "is this going
to work?" — before they kick off a multi-hour run only to discover
post-mortem that 98% of bronze never reached embed.

PLINTH consumes the same checks for its self-healing loop — pattern-
matching on ``check.name + check.status`` lets the trace primitive
propose specific remediation skills.
"""

from __future__ import annotations

import importlib
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Status(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class VerifyCheck:
    name: str
    status: Status
    detail: str = ""
    remediation: str | None = None


@dataclass(frozen=True)
class VerifyReport:
    checks: tuple[VerifyCheck, ...]
    total: int
    passed: int
    warned: int
    failed: int
    overall: Status


# -- probes (injectable for tests) -------------------------------------------


def _try_import(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


def _which(name: str) -> str | None:
    return shutil.which(name)


def _box_api_probe(token: str) -> int:
    try:
        import requests
        r = requests.get(
            "https://api.box.com/2.0/folders/0?limit=1",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        return r.status_code
    except Exception:  # noqa: BLE001
        return 0


def _pg_probe(dsn: str) -> bool:
    try:
        import psycopg2
        c = psycopg2.connect(dsn, connect_timeout=5)
        c.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _query_tables(dsn: str) -> list[str]:
    try:
        import psycopg2
        c = psycopg2.connect(dsn, connect_timeout=5)
        try:
            with c.cursor() as cur:
                cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                return [r[0].lower() for r in cur.fetchall()]
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        return []


# -- checks ------------------------------------------------------------------


def check_python_deps(deps: Iterable[str]) -> VerifyCheck:
    deps_list = list(deps)
    missing = [d for d in deps_list if not _try_import(d)]
    if not missing:
        return VerifyCheck(name="python_deps", status=Status.PASS,
                           detail=f"all importable: {deps_list}")
    return VerifyCheck(
        name="python_deps", status=Status.FAIL,
        detail=f"missing: {missing}",
        remediation=f"pip install --target=/var/lib/axiom/bronze/.pyextras {' '.join(missing)}",
    )


def check_tesseract_binary() -> VerifyCheck:
    path = _which("tesseract")
    if path:
        return VerifyCheck(name="tesseract_binary", status=Status.PASS,
                           detail=f"found at {path}")
    return VerifyCheck(
        name="tesseract_binary", status=Status.FAIL,
        detail="tesseract binary not in PATH",
        remediation="apt-get install -y tesseract-ocr (or bake into chart)",
    )


def check_box_auth() -> VerifyCheck:
    if os.environ.get("BOX_JWT_CONFIG"):
        return VerifyCheck(
            name="box_auth", status=Status.PASS,
            detail="BOX_JWT_CONFIG present; JWT mint not exercised in fast pre-flight",
        )
    token = os.environ.get("BOX_DEVELOPER_TOKEN")
    if not token:
        return VerifyCheck(
            name="box_auth", status=Status.FAIL, detail="no credentials",
            remediation="Set BOX_JWT_CONFIG (production) or BOX_DEVELOPER_TOKEN (60-min dev)",
        )
    code = _box_api_probe(token)
    if code == 200:
        return VerifyCheck(name="box_auth", status=Status.PASS,
                           detail="HTTP 200 from /folders/0 — dev token works")
    if code == 401:
        return VerifyCheck(
            name="box_auth", status=Status.FAIL,
            detail="HTTP 401 — token expired",
            remediation="Generate a fresh BOX_DEVELOPER_TOKEN or wire JWT auth",
        )
    return VerifyCheck(
        name="box_auth", status=Status.FAIL,
        detail=f"HTTP {code} from Box",
        remediation="Check network + token + rate-limit window",
    )


def check_pg_dsn() -> VerifyCheck:
    dsn = os.environ.get("DP1_RAG_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        return VerifyCheck(
            name="pg_dsn", status=Status.FAIL,
            detail="neither DP1_RAG_DSN nor DATABASE_URL set",
            remediation="Set DP1_RAG_DSN to the cluster Postgres DSN",
        )
    if _pg_probe(dsn):
        return VerifyCheck(name="pg_dsn", status=Status.PASS, detail="connect OK")
    return VerifyCheck(
        name="pg_dsn", status=Status.FAIL, detail="connect failed",
        remediation="Verify password (k8s secret), pod IP, port-forward",
    )


def check_rag_schema(dsn: str) -> VerifyCheck:
    tables = _query_tables(dsn)
    required = {"chunks", "documents"}
    missing = required - set(tables)
    if not missing:
        return VerifyCheck(name="rag_schema", status=Status.PASS,
                           detail=f"tables present: {sorted(required)}")
    return VerifyCheck(
        name="rag_schema", status=Status.FAIL,
        detail=f"missing tables: {sorted(missing)}",
        remediation="Run RAGStore.connect() once to bootstrap the schema",
    )


# -- aggregator --------------------------------------------------------------


def run_all_checks(checks: Iterable[VerifyCheck] | None = None) -> VerifyReport:
    """Aggregate pre-flight checks into a :class:`VerifyReport`.

    ``checks=None`` runs the standard pre-flight battery; pass an
    explicit list to compose custom batteries (PLINTH does this).
    """
    if checks is None:
        checks = [
            check_python_deps(["pypdf", "pypdfium2", "pytesseract", "requests"]),
            check_tesseract_binary(),
            check_box_auth(),
            check_pg_dsn(),
        ]
        dsn = os.environ.get("DP1_RAG_DSN") or os.environ.get("DATABASE_URL")
        if dsn:
            checks.append(check_rag_schema(dsn))

    checks_t = tuple(checks)
    passed = sum(1 for c in checks_t if c.status == Status.PASS)
    warned = sum(1 for c in checks_t if c.status == Status.WARN)
    failed = sum(1 for c in checks_t if c.status == Status.FAIL)

    if failed > 0:
        overall = Status.FAIL
    elif warned > 0:
        overall = Status.WARN
    else:
        overall = Status.PASS

    return VerifyReport(
        checks=checks_t,
        total=len(checks_t),
        passed=passed,
        warned=warned,
        failed=failed,
        overall=overall,
    )


__all__ = [
    "Status", "VerifyCheck", "VerifyReport",
    "check_python_deps", "check_tesseract_binary", "check_box_auth",
    "check_pg_dsn", "check_rag_schema", "run_all_checks",
]
