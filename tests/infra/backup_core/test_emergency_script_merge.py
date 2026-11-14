# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One emergency script (``axi-restore.py state|vault``); the old names are thin shims for a release.

The full broken-install behaviour of each subcommand is tested where it always was
(``tests/infra/test_state_snapshots.py`` and ``tests/infra/vault_backup/test_emergency_script.py``,
both now driving ``axi-restore.py``); this file proves the merge itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from axiom.infra import state_snapshots as ss

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
MERGED = SCRIPTS / "axi-restore.py"


def _isolated(script: Path, *args, cwd: Path, extra_path: Path | None = None):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    if extra_path:
        env["PYTHONPATH"] = str(extra_path)
    return subprocess.run([sys.executable, "-I", str(script), *args], capture_output=True, text=True,
                          cwd=cwd, env=env, timeout=120)


@pytest.fixture
def shadow(tmp_path):
    d = tmp_path / "shadow"
    (d / "axiom").mkdir(parents=True)
    (d / "axiom" / "__init__.py").write_text("raise ImportError('axiom is broken (shadow)')\n")
    return d


@pytest.fixture
def populated(state_dir, tmp_path):
    (state_dir / "identity").mkdir()
    (state_dir / "settings.toml").write_text("[a]\nb = 1\n")
    (state_dir / "identity" / "node.json").write_text('{"k": 1}')
    ss.create_snapshot(state_dir, state_dir / "snapshots", policy=ss.SnapshotPolicy(), force=True)
    return state_dir


def test_there_is_one_real_script_and_two_thin_shims():
    assert MERGED.is_file()
    for name in ("axi-state-restore.py", "axi-vault-restore.py"):
        shim = (SCRIPTS / name).read_text()
        assert len(shim.splitlines()) < 40 and "axi-restore.py" in shim
        assert "def " not in shim  # no logic of its own


@pytest.mark.parametrize("name", ["axi-restore.py", "axi-state-restore.py", "axi-vault-restore.py"])
def test_every_name_runs_with_axiom_broken(name, shadow, populated, tmp_path):
    args = ["state", "list"] if name == "axi-restore.py" else ["list"]
    if name == "axi-vault-restore.py":
        junk = tmp_path / "junk.axvb"
        junk.write_bytes(b"not a backup")
        args = ["list", str(junk)]
    if name != "axi-vault-restore.py":
        args += ["--state", str(populated)]
    out = _isolated(SCRIPTS / name, *args, cwd=shadow, extra_path=shadow)
    assert "axiom is broken" not in out.stderr and "Traceback" not in out.stderr, out.stderr
    # a junk archive named on the command line is refused (non-zero, on stderr); everything else lists cleanly
    assert out.returncode == (1 if name == "axi-vault-restore.py" else 0), out.stdout + out.stderr
    if name == "axi-vault-restore.py":
        assert "not a vault backup" in out.stderr


def test_the_state_shim_restores_exactly_like_the_merged_script(populated, tmp_path, shadow):
    (populated / "settings.toml").write_text("damaged")
    out = _isolated(SCRIPTS / "axi-state-restore.py", "restore", "latest", "--state", str(populated), "--apply",
                    cwd=shadow, extra_path=shadow)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (populated / "settings.toml").read_text() == "[a]\nb = 1\n"
    assert "axi-restore.py state" in out.stderr  # the deprecation note names the new spelling


def test_the_state_subcommands_do_not_need_cryptography(populated, tmp_path, shadow):
    """`state` must work on a machine that lacks the vault's one third-party dependency."""
    blocker = tmp_path / "blocker"
    (blocker / "cryptography").mkdir(parents=True)
    (blocker / "cryptography" / "__init__.py").write_text("raise ImportError('no cryptography here')\n")
    src = tmp_path / "run.py"
    src.write_text(
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(blocker)!r})\n"
        f"sys.argv = ['axi-restore.py', 'state', 'verify', 'latest', '--state', {str(populated)!r}]\n"
        f"runpy.run_path({str(MERGED)!r}, run_name='__main__')\n")
    out = subprocess.run([sys.executable, "-I", str(src)], capture_output=True, text=True, cwd=shadow, timeout=120)
    assert out.returncode == 0 and "OK" in out.stdout, out.stdout + out.stderr


def test_the_published_copy_beside_snapshots_is_the_merged_script(populated):
    copy = populated / "snapshots" / "axi-restore.py"
    assert copy.read_bytes() == MERGED.read_bytes() and os.access(copy, os.X_OK)
    assert not (populated / "snapshots" / "axi-state-restore.py").exists()


def test_an_old_snapshot_dir_still_holds_a_working_old_script(tmp_path):
    """Snapshot dirs made before the merge carry a complete copy of the old script: untouched."""
    fx = Path(__file__).parent / "fixtures" / "snapshots_v1"
    root = tmp_path / "old"
    shutil.copytree(fx, root)
    new = _isolated(MERGED, "state", "verify", "latest", "--state", str(tmp_path / "nostate"),
                    "--snapshots", str(root), cwd=tmp_path)
    assert new.returncode == 0, new.stdout + new.stderr
