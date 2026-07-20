# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI subprocess smoke tests for ``axi gate``.

Per ``feedback_cli_subprocess_smoke_required``: every CLI verb gets an
end-to-end test that runs the module as a subprocess and asserts on stdout.
Catches entry-point/import bugs + verb-format drift that unit tests miss.
"""

from __future__ import annotations

import json as _json
import os
import subprocess
import sys
from pathlib import Path

PYTHON = sys.executable
MOD = "axiom.extensions.builtins.webgate"
# Test THIS tree, not whatever `axiom` the venv is editable-anchored to — the
# workspace can have a stale sibling checkout installed (see the dev-env
# collision hazard). Prepending src/ makes `python -m` import the code here.
_SRC = Path(__file__).resolve().parents[5]


def _run(argv: list[str], *, state_dir: Path):
    env = {**os.environ, "AXI_STATE_DIR": str(state_dir)}
    env.pop("AXIOM_GATE_USERS_FILE", None)  # isolate from any node config
    env.pop("AXIOM_GATE_API_KEYS_FILE", None)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_SRC), env.get("PYTHONPATH", "")) if p)
    return subprocess.run(
        [PYTHON, "-m", MOD, *argv],
        env=env, capture_output=True, text=True, timeout=60,
    )


# ---------- help ----------------------------------------------------------


def test_help_lists_all_verbs(tmp_path: Path):
    r = _run(["--help"], state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    for verb in ("adduser", "resetpw", "list", "issue", "revoke"):
        assert verb in r.stdout, f"missing verb {verb!r} in --help"


# ---------- adduser → list round-trip -------------------------------------


def test_adduser_then_list(tmp_path: Path):
    f = str(tmp_path / "gate-users.json")
    add = _run(["--accounts-file", f, "adduser", "op@ut.example",
                "--password", "Correct-Horse-9", "--role", "operator",
                "--name", "Op Name"], state_dir=tmp_path)
    assert add.returncode == 0, add.stderr
    assert "op@ut.example" in add.stdout

    lst = _run(["--json", "--accounts-file", f, "list"], state_dir=tmp_path)
    assert lst.returncode == 0, lst.stderr
    payload = _json.loads(lst.stdout)
    items = payload["value"]["items"]
    assert [it["email"] for it in items] == ["op@ut.example"]
    assert items[0]["roles"] == ["operator"]
    # a hash must never surface in list output
    assert "password_hash" not in lst.stdout


def test_adduser_generates_and_prints_password(tmp_path: Path):
    f = str(tmp_path / "gate-users.json")
    r = _run(["--accounts-file", f, "adduser", "gen@ut.example",
              "--role", "student"], state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "temporary password" in r.stdout.lower()


def test_resetpw_round_trip(tmp_path: Path):
    f = str(tmp_path / "gate-users.json")
    _run(["--accounts-file", f, "adduser", "rot@ut.example",
          "--password", "Correct-Horse-9", "--role", "operator"], state_dir=tmp_path)
    rst = _run(["--json", "--accounts-file", f, "resetpw", "rot@ut.example",
                "--password", "Brand-New-9"], state_dir=tmp_path)
    assert rst.returncode == 0, rst.stderr
    assert _json.loads(rst.stdout)["ok"] is True


# ---------- failure exit codes --------------------------------------------


def test_adduser_duplicate_exits_nonzero(tmp_path: Path):
    f = str(tmp_path / "gate-users.json")
    args = ["--accounts-file", f, "adduser", "dup@ut.example",
            "--password", "Correct-Horse-9"]
    assert _run(args, state_dir=tmp_path).returncode == 0
    dup = _run(args, state_dir=tmp_path)
    assert dup.returncode != 0
    assert "already exists" in dup.stderr


def test_list_missing_file_is_empty_ok(tmp_path: Path):
    f = str(tmp_path / "gate-users.json")
    r = _run(["--accounts-file", f, "list"], state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "no accounts" in r.stdout.lower()


# ---------- API keys: issue → list → revoke round-trip ---------------------


def test_issue_list_revoke_api_key_round_trip(tmp_path: Path):
    f = str(tmp_path / "gate-api-keys.json")
    issued = _run(["--json", "--keys-file", f, "issue", "api-key",
                   "--principal", "@svc:org",
                   "--scope", "llm", "--scope", "rag:read",
                   "--name", "backend service"], state_dir=tmp_path)
    assert issued.returncode == 0, issued.stderr
    payload = _json.loads(issued.stdout)
    token = payload["value"]["token"]
    key_id = payload["value"]["key_id"]
    assert token.startswith("axk_")

    lst = _run(["--json", "--keys-file", f, "list", "api-keys"],
               state_dir=tmp_path)
    assert lst.returncode == 0, lst.stderr
    items = _json.loads(lst.stdout)["value"]["items"]
    assert [it["key_id"] for it in items] == [key_id]
    assert items[0]["principal"] == "@svc:org"
    assert "secret_hash" not in lst.stdout and token not in lst.stdout

    rev = _run(["--keys-file", f, "revoke", "api-key", key_id],
               state_dir=tmp_path)
    assert rev.returncode == 0, rev.stderr

    lst2 = _run(["--json", "--keys-file", f, "list", "api-keys"],
                state_dir=tmp_path)
    assert _json.loads(lst2.stdout)["value"]["items"][0]["revoked_at"]


def test_issue_requires_scope_flag(tmp_path: Path):
    f = str(tmp_path / "gate-api-keys.json")
    r = _run(["--keys-file", f, "issue", "api-key",
              "--principal", "@svc:org"], state_dir=tmp_path)
    assert r.returncode != 0


def test_revoke_unknown_key_exits_nonzero(tmp_path: Path):
    f = str(tmp_path / "gate-api-keys.json")
    _run(["--keys-file", f, "issue", "api-key", "--principal", "@svc:org",
          "--scope", "llm"], state_dir=tmp_path)
    r = _run(["--keys-file", f, "revoke", "api-key", "ghost"],
             state_dir=tmp_path)
    assert r.returncode != 0
    assert "no such key" in r.stderr
