# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A test run must not be able to reach a developer's live database or action ledger by default.

Three times in one week a test wrote into the live local stack because it forgot to bind a seam: memory
fragments, append-only ledger rows (which cannot be removed afterwards) and per-worker schemas. The root
conftest now points the process at an address that refuses connections when nobody chose a database.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run(env_overrides: dict, code: str) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"AXIOM_DB_URL", "AXIOM_ACTION_LEDGER_BACKEND", "PYTEST_CURRENT_TEST"}
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ROOT, timeout=120
    )


def test_with_no_database_chosen_the_default_is_a_refusing_address():
    """Run a throwaway pytest session in a subprocess with the variable unset, and read what it saw."""
    probe = textwrap.dedent(
        """
        import os, pytest
        def test_probe():
            print("DB=" + os.environ.get("AXIOM_DB_URL", ""))
            print("LEDGER=" + os.environ.get("AXIOM_ACTION_LEDGER_BACKEND", ""))
        """
    )
    d = ROOT / "tests" / "_isolation_probe_tmp"
    d.mkdir(exist_ok=True)
    try:
        (d / "test_probe.py").write_text(probe)
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(d / "test_probe.py"),
                "-q",
                "-s",
                "-p",
                "no:cacheprovider",
                "-m",
                "",
                "-n",
                "0",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=180,
            env={
                k: v
                for k, v in os.environ.items()
                if k not in {"AXIOM_DB_URL", "AXIOM_ACTION_LEDGER_BACKEND", "PYTEST_CURRENT_TEST"}
            },
        )
    finally:
        shutil.rmtree(d, ignore_errors=True)
    out = r.stdout
    assert "DB=postgresql+psycopg2://axiom:axiom@127.0.0.1:9/axiom_db" in out, out[-800:]
    assert "LEDGER=jsonl" in out, out[-800:]


def test_an_explicit_database_is_left_alone():
    """CI's service container and a developer's chosen scratch server must keep working."""
    probe = textwrap.dedent(
        """
        import os
        def test_probe():
            print("DB=" + os.environ.get("AXIOM_DB_URL", ""))
        """
    )
    d = ROOT / "tests" / "_isolation_probe_tmp2"
    d.mkdir(exist_ok=True)
    try:
        (d / "test_probe.py").write_text(probe)
        env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["AXIOM_DB_URL"] = "postgresql+psycopg2://scratch:scratch@db.example.invalid:5432/chosen"
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(d / "test_probe.py"),
                "-q",
                "-s",
                "-p",
                "no:cacheprovider",
                "-m",
                "",
                "-n",
                "0",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=180,
            env=env,
        )
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert "DB=postgresql+psycopg2://scratch:scratch@db.example.invalid:5432/chosen" in r.stdout, (
        r.stdout[-800:]
    )


def test_the_refusing_address_really_refuses():
    psycopg2 = pytest.importorskip("psycopg2")
    from tests._isolation import REFUSING_DB_URL as _REFUSING_DB_URL

    url = _REFUSING_DB_URL.replace("postgresql+psycopg2://", "postgresql://")
    with pytest.raises(psycopg2.OperationalError):
        psycopg2.connect(url, connect_timeout=3)
