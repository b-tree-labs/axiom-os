# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI subprocess smokes for the tabular kinds (ADR-001 P2).

Per ``feedback_cli_subprocess_smoke_required``. These pin the subprocess to the
repository's ``src`` on PYTHONPATH so the smoke exercises THIS checkout, not a
stale editable-installed sibling (``feedback_prepush_validates_stale_sibling``).
No network / DB: register only validates + persists; it never connects.
"""

from __future__ import annotations

import json as _json
import os
import subprocess
import sys
from pathlib import Path

PYTHON = sys.executable
MOD = "axiom.extensions.builtins.data_platform"
_SRC = Path(__file__).resolve().parents[5]  # .../axiom-srcurl/src


def _run(argv: list[str], *, state_dir: Path):
    env = {**os.environ, "AXI_STATE_DIR": str(state_dir), "PYTHONPATH": str(_SRC)}
    return subprocess.run(
        [PYTHON, "-m", MOD, *argv],
        env=env, capture_output=True, text=True, timeout=30,
    )


def test_list_kinds_includes_both_tabular_kinds(tmp_path: Path):
    r = _run(["--json", "list", "kinds"], state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    kinds = {item["kind"] for item in _json.loads(r.stdout)["value"]["items"]}
    assert {"http-tabular", "sql-tabular"} <= kinds, kinds


def test_register_sql_tabular_help_shows_query_flag(tmp_path: Path):
    r = _run(["register", "p", "sql-tabular", "--help"], state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--query" in r.stdout and "--schema-ref" in r.stdout


def test_register_help_shows_platform_credential_ref(tmp_path: Path):
    r = _run(["register", "--help"], state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--credential-ref" in r.stdout


def test_register_sql_tabular_round_trip(tmp_path: Path):
    r = _run([
        "--json", "register", "preds",
        "--bronze-root", str(tmp_path / "b"),
        "--default-disposition", "allow",
        "--credential-ref", "env://SHADOW_DB_DSN",
        "sql-tabular",
        "--query", "SELECT d, v FROM series",
        "--schema-ref", "preds.series.v1",
    ], state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    payload = _json.loads(r.stdout)
    assert payload["ok"], payload
    # the connector persisted with the platform-generic credential_ref
    toml = (tmp_path / "plinth" / "connectors" / "preds.toml").read_text()
    assert 'kind = "sql-tabular"' in toml
    assert 'credential_ref = "env://SHADOW_DB_DSN"' in toml
    assert "SELECT d, v FROM series" in toml


def test_register_sql_tabular_without_credential_ref_fails(tmp_path: Path):
    r = _run([
        "--json", "register", "preds2",
        "--bronze-root", str(tmp_path / "b"),
        "sql-tabular",
        "--query", "SELECT 1",
        "--schema-ref", "s.v1",
    ], state_dir=tmp_path)
    # validate() rejects a sql-tabular connector with no DSN credential
    payload = _json.loads(r.stdout)
    assert not payload["ok"]
    assert any("--credential-ref" in e for e in payload["errors"])


_GOOD_MAP = (
    '[promotion]\ntarget = "gold.series"\nnatural_key = ["obs_date"]\n'
    '[promotion.columns]\nobs_date = "d"\nv = "v"\n'
)
_BAD_MAP = '[promotion]\ntarget = "gold.series"\nnatural_key = ["obs_date"]\n[promotion.columns]\nv = "v"\n'


def test_register_with_valid_promotion_map_persists_it(tmp_path: Path):
    m = tmp_path / "map.toml"
    m.write_text(_GOOD_MAP)
    r = _run([
        "--json", "register", "feed",
        "--bronze-root", str(tmp_path / "b"),
        "--promotion-map-file", str(m),
        "http-tabular", "--url", "https://x/d.csv", "--format", "csv", "--schema-ref", "s.v1",
    ], state_dir=tmp_path)
    assert _json.loads(r.stdout)["ok"], r.stdout
    toml = (tmp_path / "plinth" / "connectors" / "feed.toml").read_text()
    assert "promotion_map_file" in toml


def test_register_with_bad_promotion_map_fails_at_register(tmp_path: Path):
    m = tmp_path / "map.toml"
    m.write_text(_BAD_MAP)  # natural_key not in columns
    r = _run([
        "--json", "register", "feed2",
        "--bronze-root", str(tmp_path / "b"),
        "--promotion-map-file", str(m),
        "http-tabular", "--url", "https://x/d.csv", "--format", "csv", "--schema-ref", "s.v1",
    ], state_dir=tmp_path)
    payload = _json.loads(r.stdout)
    assert not payload["ok"]
    assert any("natural_key column 'obs_date'" in e for e in payload["errors"])
