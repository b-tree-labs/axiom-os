# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Extraction-dependency gap: declared extra + loud diagnose.

Document ingestion needs OCR/PDF/office-doc libs (the ``extraction`` extra).
A missing one used to degrade ingestion to text-only or fail quietly mid-run.
These tests pin the importable preflight, the diagnose finding, and the loud
ingest warning so the gap can't silently reopen.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from axiom.extensions.builtins.data_platform.skills import diagnose, ingest, verify


def _find_pyproject() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "pyproject.toml"
        if candidate.exists():
            return candidate
    return None


def _extraction_extra_dists() -> set[str]:
    """Distribution names (sans version/extras markers) in the `extraction`
    extra. Skips if pyproject isn't on disk (e.g. installed wheel)."""
    pp = _find_pyproject()
    if pp is None:
        pytest.skip("pyproject.toml not on disk (installed wheel)")
    data = tomllib.loads(pp.read_text())
    reqs = data["project"]["optional-dependencies"]["extraction"]
    out: set[str] = set()
    for req in reqs:
        # "python-pptx>=0.6.21" -> "python-pptx"; "boxsdk[jwt]>=3.9" -> "boxsdk[jwt]"
        name = req.split(">")[0].split("=")[0].split("<")[0].split("~")[0].strip()
        out.add(name)
    return out


# -- importable preflight ----------------------------------------------------


def test_extraction_deps_listed_match_pyproject_extra():
    # Import names the extract/OCR/office-doc path actually pulls in.
    imports = {imp for imp, _dist in verify.EXTRACTION_DEPS}
    assert imports == {
        "boxsdk", "pypdf", "pypdfium2", "pytesseract", "docx", "pptx", "openpyxl",
    }
    # The dists named by the check must equal the `extraction` extra in
    # pyproject (drift here is exactly the silent-gap class this fixes).
    extra = _extraction_extra_dists()
    check_dists = {dist for _imp, dist in verify.EXTRACTION_DEPS}
    assert check_dists == extra


def test_missing_extraction_deps_none_when_all_importable():
    with patch.object(verify, "_try_import", return_value=True):
        assert verify.missing_extraction_deps() == []


def test_missing_extraction_deps_reports_each_absent():
    with patch.object(verify, "_try_import", side_effect=lambda n: n != "boxsdk"):
        assert verify.missing_extraction_deps() == ["boxsdk"]


def test_check_extraction_deps_pass():
    with patch.object(verify, "_try_import", return_value=True):
        c = verify.check_extraction_deps()
    assert c.status is verify.Status.PASS


def test_check_extraction_deps_fail_is_actionable():
    with patch.object(verify, "_try_import", return_value=False):
        c = verify.check_extraction_deps()
    assert c.status is verify.Status.FAIL
    assert c.remediation == verify.EXTRACTION_REMEDIATION
    assert "pip install 'axiom-os-lm[extraction]'" in c.remediation
    # names the missing dist so the operator knows what's gone
    assert "python-docx" in c.detail


def test_check_extraction_deps_in_standard_battery():
    with patch.object(verify, "_box_api_probe", return_value=0):
        report = verify.run_all_checks()
    assert any(c.name == "extraction_deps" for c in report.checks)


# -- loud diagnose -----------------------------------------------------------


def test_diagnose_surfaces_missing_extraction_without_kubectl():
    # No cluster on a dev box, but the local extraction check must still fire.
    with (
        patch.object(verify, "_try_import", return_value=False),
        patch("axiom.extensions.builtins.data_platform.skills.diagnose.shutil.which", return_value=None),
    ):
        result = diagnose.run({}, ctx=object())  # ctx unused on this path
    assert result.ok is False
    assert verify.EXTRACTION_REMEDIATION in result.errors
    findings = result.value["findings"]
    assert findings[0]["check"] == "extraction_deps"
    assert findings[0]["ok"] is False


def test_diagnose_extraction_ok_does_not_add_remediation_error():
    with (
        patch.object(verify, "_try_import", return_value=True),
        patch("axiom.extensions.builtins.data_platform.skills.diagnose.shutil.which", return_value=None),
    ):
        result = diagnose.run({}, ctx=object())
    # Only the kubectl error, not the extraction remediation.
    assert result.errors == ["kubectl not on PATH"]


# -- loud ingest warning -----------------------------------------------------


class _Report:
    proceed = True
    items_seen = 0
    items_landed = 0
    items_failed = 0
    refused_reason = ""
    connector = "demo"
    funnel = None


class _Ctx:
    state_dir = None


def test_ingest_warns_loudly_when_extraction_deps_missing(caplog):
    with (
        patch.object(verify, "missing_extraction_deps", return_value=["pytesseract"]),
        patch.object(ingest, "run_connector_ingest", return_value=_Report()),
        patch.object(ingest._authz, "action") as act,
    ):
        act.return_value.__enter__.return_value.receipt_id = "r1"
        with caplog.at_level(logging.WARNING):
            result = ingest.run({"connector": "demo"}, _Ctx())

    assert any("axiom-os-lm[extraction]" in m for m in caplog.messages)
    assert any("WARNING" in a and "axiom-os-lm[extraction]" in a for a in result.actions_taken)


def test_ingest_no_warning_when_deps_present(caplog):
    with (
        patch.object(verify, "missing_extraction_deps", return_value=[]),
        patch.object(ingest, "run_connector_ingest", return_value=_Report()),
        patch.object(ingest._authz, "action") as act,
    ):
        act.return_value.__enter__.return_value.receipt_id = "r1"
        with caplog.at_level(logging.WARNING):
            ingest.run({"connector": "demo"}, _Ctx())

    assert not any("axiom-os-lm[extraction]" in m for m in caplog.messages)
