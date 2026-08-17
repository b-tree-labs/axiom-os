# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi data verify`` — pre-flight health check skill.

Tonight's stand-up hit ~10 failure patterns that all looked like
SUCCESS at the operator surface. ``verify`` runs every check at the
exact place operators ask "is my pipeline going to work?" — daemon
deps, PYTHONPATH, OCR binaries, Box auth, PG reachability, rag
schema, provenance rules.

Returns a structured ``VerifyReport`` with per-check pass/fail and
operator-actionable error messages.
"""

from __future__ import annotations

from unittest.mock import patch


from axiom.extensions.builtins.data_platform.skills.verify import (
    VerifyCheck,
    VerifyReport,
    Status,
    check_python_deps,
    check_tesseract_binary,
    check_box_auth,
    check_pg_dsn,
    check_rag_schema,
    run_all_checks,
)


# -- VerifyCheck primitive ---------------------------------------------------


def test_verify_check_pass_shape():
    c = VerifyCheck(name="example", status=Status.PASS, detail="all good")
    assert c.status == Status.PASS
    assert c.detail == "all good"


def test_verify_check_fail_carries_actionable_detail():
    c = VerifyCheck(name="example", status=Status.FAIL,
                    detail="thing X is missing",
                    remediation="run `pip install X`")
    assert c.remediation == "run `pip install X`"


# -- python deps -------------------------------------------------------------


def test_check_python_deps_pass_when_all_importable():
    with patch("axiom.extensions.builtins.data_platform.skills.verify._try_import",
               return_value=True):
        c = check_python_deps(["pypdf", "pypdfium2", "pytesseract"])
    assert c.status == Status.PASS


def test_check_python_deps_fail_lists_missing(monkeypatch):
    from axiom.extensions.builtins.data_platform.skills import verify as v
    missing_set = {"pypdfium2", "pytesseract"}
    def fake(name):
        return name not in missing_set
    monkeypatch.setattr(v, "_try_import", fake)
    c = check_python_deps(["pypdf", "pypdfium2", "pytesseract"])
    assert c.status == Status.FAIL
    assert "pypdfium2" in c.detail
    assert "pytesseract" in c.detail
    assert "pip install" in (c.remediation or "")


# -- tesseract binary --------------------------------------------------------


def test_check_tesseract_pass_when_present(monkeypatch):
    from axiom.extensions.builtins.data_platform.skills import verify as v
    monkeypatch.setattr(v, "_which", lambda n: "/usr/bin/tesseract")
    c = check_tesseract_binary()
    assert c.status == Status.PASS


def test_check_tesseract_fail_when_absent(monkeypatch):
    from axiom.extensions.builtins.data_platform.skills import verify as v
    monkeypatch.setattr(v, "_which", lambda n: None)
    c = check_tesseract_binary()
    assert c.status == Status.FAIL
    assert "tesseract" in c.remediation


# -- Box auth ----------------------------------------------------------------


def test_check_box_auth_pass_when_jwt_works(monkeypatch):
    from axiom.extensions.builtins.data_platform.skills import verify as v
    monkeypatch.setenv("BOX_JWT_CONFIG", '{"fake":"config"}')
    with patch.object(v, "_box_api_probe", return_value=200):
        c = check_box_auth()
    assert c.status == Status.PASS


def test_check_box_auth_pass_when_dev_token_valid(monkeypatch):
    from axiom.extensions.builtins.data_platform.skills import verify as v
    monkeypatch.delenv("BOX_JWT_CONFIG", raising=False)
    monkeypatch.setenv("BOX_DEVELOPER_TOKEN", "valid-token")
    with patch.object(v, "_box_api_probe", return_value=200):
        c = check_box_auth()
    assert c.status == Status.PASS


def test_check_box_auth_fail_no_credentials(monkeypatch):
    monkeypatch.delenv("BOX_JWT_CONFIG", raising=False)
    monkeypatch.delenv("BOX_DEVELOPER_TOKEN", raising=False)
    c = check_box_auth()
    assert c.status == Status.FAIL
    assert "BOX_JWT_CONFIG" in (c.remediation or "") or "BOX_DEVELOPER_TOKEN" in (c.remediation or "")


def test_check_box_auth_fail_401(monkeypatch):
    from axiom.extensions.builtins.data_platform.skills import verify as v
    monkeypatch.setenv("BOX_DEVELOPER_TOKEN", "expired")
    with patch.object(v, "_box_api_probe", return_value=401):
        c = check_box_auth()
    assert c.status == Status.FAIL
    assert "401" in c.detail


# -- PG DSN ------------------------------------------------------------------


def test_check_pg_dsn_pass(monkeypatch):
    from axiom.extensions.builtins.data_platform.skills import verify as v
    monkeypatch.setenv("DP1_RAG_DSN", "postgresql://fake")
    with patch.object(v, "_pg_probe", return_value=True):
        c = check_pg_dsn()
    assert c.status == Status.PASS


def test_check_pg_dsn_fail_unset(monkeypatch):
    monkeypatch.delenv("DP1_RAG_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = check_pg_dsn()
    assert c.status == Status.FAIL


# -- rag schema --------------------------------------------------------------


def test_check_rag_schema_pass(monkeypatch):
    from axiom.extensions.builtins.data_platform.skills import verify as v
    with patch.object(v, "_query_tables", return_value=["chunks", "documents"]):
        c = check_rag_schema("postgresql://fake")
    assert c.status == Status.PASS


def test_check_rag_schema_fail_missing(monkeypatch):
    from axiom.extensions.builtins.data_platform.skills import verify as v
    with patch.object(v, "_query_tables", return_value=["chunks"]):
        c = check_rag_schema("postgresql://fake")
    assert c.status == Status.FAIL
    assert "documents" in c.detail


# -- run_all_checks aggregates -----------------------------------------------


def test_run_all_checks_returns_report():
    report = run_all_checks(checks=[
        VerifyCheck(name="a", status=Status.PASS),
        VerifyCheck(name="b", status=Status.PASS),
    ])
    assert isinstance(report, VerifyReport)
    assert report.total == 2
    assert report.passed == 2


def test_run_all_checks_overall_status_fail_when_any_fails():
    report = run_all_checks(checks=[
        VerifyCheck(name="a", status=Status.PASS),
        VerifyCheck(name="b", status=Status.FAIL, detail="x", remediation="y"),
    ])
    assert report.overall == Status.FAIL
    assert report.failed == 1


def test_run_all_checks_overall_warn_when_no_fails_but_warns():
    report = run_all_checks(checks=[
        VerifyCheck(name="a", status=Status.PASS),
        VerifyCheck(name="b", status=Status.WARN, detail="..."),
    ])
    assert report.overall == Status.WARN
