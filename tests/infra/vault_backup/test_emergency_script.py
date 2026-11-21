# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""scripts/axi-restore.py (vault subcommands) works with Axiom broken: `python -I`, shadowed `axiom`, no imports."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from axiom.infra import vault_backup as vb

from .conftest import CANARY_A, CANARY_B, PASSPHRASE

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "axi-restore.py"


@pytest.fixture
def broken_axiom(tmp_path):
    """A directory whose `axiom` package raises on import, placed where Python would find it."""
    d = tmp_path / "shadow"
    (d / "axiom").mkdir(parents=True)
    (d / "axiom" / "__init__.py").write_text("raise ImportError('axiom is broken (shadow)')\n")
    return d


def _run(broken, *args, stdin=None):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONPATH"] = str(broken)  # ignored by -I; present to prove -I does the isolating
    return subprocess.run([sys.executable, "-I", str(SCRIPT), "vault", *args], cwd=broken, env=env,
                          capture_output=True, text=True, input=stdin, timeout=120)


def test_the_shadow_really_breaks_axiom(broken_axiom):
    env = dict(os.environ, PYTHONPATH=str(broken_axiom))
    r = subprocess.run([sys.executable, "-c", "import axiom"], cwd=broken_axiom, env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "shadow" in r.stderr


def test_the_script_imports_nothing_from_axiom():
    src = SCRIPT.read_text()
    assert "import axiom" not in src and "from axiom" not in src


def _code_file(tmp_path, code):
    f = tmp_path / "code.txt"
    f.write_text(code + "\n")
    f.chmod(0o600)
    return f


def test_list_needs_no_key_and_prints_no_values(state_dir, vault, root, keys, broken_axiom):
    r = vb.create_backup(state_dir)
    out = _run(broken_axiom, "list", r.path)
    assert out.returncode == 0, out.stderr
    assert "alpha-token" in out.stdout and "bravo-key" in out.stdout
    assert CANARY_A not in out.stdout + out.stderr


def test_verify_and_extract_with_axiom_broken(state_dir, vault, root, keys, broken_axiom, tmp_path):
    r = vb.create_backup(state_dir)
    cf = _code_file(tmp_path, keys.code)
    v = _run(broken_axiom, "verify", r.path, "--code-file", str(cf))
    assert v.returncode == 0, v.stderr
    dest = tmp_path / "out"
    x = _run(broken_axiom, "extract", r.path, "--code-file", str(cf), "--to", str(dest))
    assert x.returncode == 0, x.stderr
    assert (dest / "alpha-token").read_text() == CANARY_A
    assert (dest / "bravo-key").read_text() == CANARY_B
    assert oct((dest / "alpha-token").stat().st_mode & 0o777) == "0o600"
    assert oct(dest.stat().st_mode & 0o777) == "0o700"
    assert CANARY_A not in x.stdout + x.stderr  # extract names files; it never prints values


def test_reveal_prints_exactly_one_value_on_request(state_dir, vault, root, keys, broken_axiom, tmp_path):
    r = vb.create_backup(state_dir)
    out = _run(broken_axiom, "reveal", r.path, "bravo-key", "--code-file", str(_code_file(tmp_path, keys.code)))
    assert out.stdout == CANARY_B and CANARY_A not in out.stdout


def test_wrong_code_gives_the_generic_error_and_writes_nothing(state_dir, vault, root, keys, broken_axiom, tmp_path):
    from axiom.infra.vault_backup import crypto as C

    r = vb.create_backup(state_dir)
    dest = tmp_path / "out"
    x = _run(broken_axiom, "extract", r.path, "--to", str(dest),
             "--code-file", str(_code_file(tmp_path, C.generate_recovery_code())))
    assert x.returncode != 0 and C.GENERIC_FAILURE in x.stderr
    assert not dest.exists()
    assert CANARY_A not in x.stdout + x.stderr


def test_damaged_and_foreign_files_are_refused(state_dir, vault, root, keys, broken_axiom, tmp_path):
    r = vb.create_backup(state_dir)
    cut = tmp_path / "cut.axvb"
    cut.write_bytes(Path(r.path).read_bytes()[:40])
    assert _run(broken_axiom, "list", str(cut)).returncode != 0
    junk = tmp_path / "junk"
    junk.write_text("hello")
    assert "not a vault backup" in _run(broken_axiom, "list", str(junk)).stderr


def test_passphrase_archives_work_too(state_dir, vault, root, fast_kdf, broken_axiom, tmp_path):
    r = vb.create_backup(state_dir, passphrase=PASSPHRASE)
    out = _run(broken_axiom, "verify", r.path, "--code-file", str(_code_file(tmp_path, PASSPHRASE)))
    assert out.returncode == 0, out.stderr


def test_a_newer_format_is_refused_with_advice(state_dir, vault, root, keys, broken_axiom):
    import json
    import struct

    from axiom.infra.vault_backup import crypto as C

    r = vb.create_backup(state_dir)
    data = Path(r.path).read_bytes()
    h, hb, off = C.read_header(data)
    h2 = json.loads(hb)
    h2["format"] = 7
    nh = C._canon(h2)
    f = Path(r.path).with_name("newer.axvb")
    f.write_bytes(C.MAGIC + struct.pack(">I", len(nh)) + nh + data[off:])
    out = _run(broken_axiom, "list", str(f))
    assert out.returncode != 0 and "newer release" in out.stderr
