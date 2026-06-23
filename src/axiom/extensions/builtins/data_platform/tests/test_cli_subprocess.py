# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI subprocess smoke tests for ``axi data`` — source-agnostic shape.

Per ``feedback_cli_subprocess_smoke_required``: every CLI verb gets an
end-to-end test that runs the module as a subprocess and asserts on
stdout. Catches entry-point bugs + verb-to-verb format drift that
unit tests miss.

Verb shape post-2026-05-30 (ADR-056 skill-as-function + source-kind
provider): imperative verbs; resources positional; no kebab-compounds;
`install` is source-agnostic; `register <name> <kind>` dispatches to
the kind's provider for kind-specific args."""

from __future__ import annotations

import json as _json
import os
import subprocess
import sys
from pathlib import Path

PYTHON = sys.executable
MOD = "axiom.extensions.builtins.data_platform"


def _run(argv: list[str], *, state_dir: Path, env_extra: dict[str, str] | None = None):
    env = {**os.environ, "AXI_STATE_DIR": str(state_dir), **(env_extra or {})}
    return subprocess.run(
        [PYTHON, "-m", MOD, *argv],
        env=env, capture_output=True, text=True, timeout=30,
    )


# ---------- help ----------------------------------------------------------


def test_cli_help_lists_all_verbs(tmp_path: Path):
    r = _run(["--help"], state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    for verb in ("install", "diagnose", "ingest", "register", "unregister", "list"):
        assert verb in r.stdout, f"missing verb {verb!r} in --help"


def test_install_help_is_source_agnostic(tmp_path: Path):
    """The platform install verb MUST NOT mention any specific source
    kind (Box, GDrive, etc.) — that's a layering violation."""
    r = _run(["install", "--help"], state_dir=tmp_path)
    assert r.returncode == 0
    assert "--namespace" in r.stdout
    assert "--kube-context" in r.stdout
    assert "--db-kind" in r.stdout
    assert "--db-mode" in r.stdout
    assert "--vector-kind" in r.stdout
    assert "--bronze-size" in r.stdout
    # source-kind-specific flags must NOT appear here
    assert "--folder-id" not in r.stdout, "Box-specific flag leaked into platform install"
    # OLTP-specific naming must NOT appear at the platform layer
    assert "--postgres-mode" not in r.stdout, "RDBMS-specific flag leaked into platform install"
    assert "--postgres-password" not in r.stdout, "RDBMS-specific flag leaked into platform install"


def test_register_help_lists_available_kinds(tmp_path: Path):
    """`axi data register --help` lists registered source kinds as
    sub-subcommands. DP-1 ships `box`; future kinds appear here without
    any platform-code change."""
    r = _run(["register", "--help"], state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "box" in r.stdout


def test_register_box_help_shows_box_specific_flags(tmp_path: Path):
    """The Box provider attaches its own flags under `register <name> box`."""
    r = _run(["register", "foo", "box", "--help"], state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--folder-id" in r.stdout
    assert "--session-path" in r.stdout


# ---------- list ----------------------------------------------------------


def test_list_connectors_empty(tmp_path: Path):
    r = _run(["list"], state_dir=tmp_path)
    assert r.returncode == 0, r.stderr


def test_list_kinds_includes_box(tmp_path: Path):
    """`axi data list kinds` enumerates registered source-kind providers."""
    r = _run(["--json", "list", "kinds"], state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    payload = _json.loads(r.stdout)
    kinds = {item["kind"] for item in payload["value"]["items"]}
    assert "box" in kinds


# ---------- register/unregister round-trip --------------------------------


def test_register_box_then_list_then_unregister(tmp_path: Path):
    reg = _run([
        "register",
        "--bronze-root", str(tmp_path / "bronze"),
        "test-box", "box",
        "--folder-id", "100",
    ], state_dir=tmp_path)
    assert reg.returncode == 0, reg.stderr
    assert "test-box" in reg.stdout

    lst = _run(["--json", "list"], state_dir=tmp_path)
    assert lst.returncode == 0
    payload = _json.loads(lst.stdout)
    names = [it["name"] for it in payload["value"]["items"]]
    assert "test-box" in names
    # generic shape: kind + params_keys, no Box-specific fields at the top
    assert payload["value"]["items"][0]["kind"] == "box"

    rm = _run(["unregister", "test-box"], state_dir=tmp_path)
    assert rm.returncode == 0
    assert "test-box" in rm.stdout


def test_register_unknown_kind_fails_clearly(tmp_path: Path):
    """Argparse rejects an unknown kind before the skill runs — no fragile
    'choose from box' tail to match, just a nonzero exit."""
    r = _run([
        "register",
        "--bronze-root", str(tmp_path / "bronze"),
        "foo", "no-such-kind",
    ], state_dir=tmp_path)
    assert r.returncode != 0
