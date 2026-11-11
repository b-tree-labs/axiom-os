# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The harness must be safe before it is useful: guard first, then damage library."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .artifacts import ARTIFACTS
from .harness import (
    DIR_DAMAGE,
    FILE_DAMAGE,
    ChaosGuardError,
    ChaosRoot,
    assert_chaos_path,
    classify,
    damage,
    run_probe,
)


class TestTheGuard:
    def test_a_path_inside_a_temp_root_is_allowed(self, tmp_path):
        root = tmp_path / "r"
        root.mkdir()
        assert assert_chaos_path(root / "a" / "b", root) == (root / "a" / "b").resolve()

    def test_a_path_outside_the_root_is_refused(self, tmp_path):
        root = tmp_path / "r"
        root.mkdir()
        with pytest.raises(ChaosGuardError):
            assert_chaos_path(tmp_path / "elsewhere", root)

    def test_the_root_itself_is_refused(self, tmp_path):
        with pytest.raises(ChaosGuardError):
            assert_chaos_path(tmp_path, tmp_path)

    def test_dotdot_escape_is_refused(self, tmp_path):
        root = tmp_path / "r"
        root.mkdir()
        with pytest.raises(ChaosGuardError):
            assert_chaos_path(root / ".." / "x", root)

    def test_a_root_that_is_not_temp_is_refused(self):
        with pytest.raises(ChaosGuardError):
            assert_chaos_path(Path.home() / ".axi" / "x", Path.home() / ".axi")

    def test_real_axi_state_is_refused_even_as_a_root_guess(self):
        with pytest.raises(ChaosGuardError):
            ChaosRoot(Path.home() / ".axi" / "chaos")

    def test_a_symlink_pointing_out_is_not_followed_to_its_target(self, tmp_path):
        root = tmp_path / "r"
        root.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("precious")
        link = root / "link"
        os.symlink(outside, link)
        damage("delete_file", link, root)  # removes the LINK, never the target
        assert outside.read_text() == "precious"
        assert not link.is_symlink()

    def test_damage_refuses_to_run_outside_the_root(self, tmp_path):
        root = tmp_path / "r"
        root.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("keep")
        with pytest.raises(ChaosGuardError):
            damage("delete_file", victim, root)
        assert victim.read_text() == "keep"

    def test_the_subprocess_env_points_everything_inside_the_root(self, tmp_path):
        chaos = ChaosRoot(tmp_path / "c")
        env = chaos.env()
        for key in ("HOME", "AXI_HOME", "AXI_STATE_DIR", "AXIOM_STATE_DIR"):
            assert Path(env[key]).resolve().is_relative_to(chaos.root.resolve()), key
        assert env["AXIOM_ACTION_LEDGER_BACKEND"] == "jsonl"
        assert ":1/" in env["AXIOM_DB_URL"]  # a port nothing listens on
        assert "AXIOM_AUDIT_HMAC_KEY" not in env

    def test_a_probe_cannot_reach_the_default_database(self, tmp_path):
        """localhost:5432 is a developer's live database; probes get a dead port."""
        chaos = ChaosRoot(tmp_path / "c")
        res = run_probe(chaos, "from axiom.infra.db import platform_db_url\nemit(platform_db_url())")
        assert res.outcome == "ok"
        assert "localhost:5432" not in res.value and ":5432" not in res.value
        probe = run_probe(chaos, """
import socket
s = socket.socket(); s.settimeout(1)
try:
    s.connect(('127.0.0.1', 1)); emit('connected')
except OSError:
    emit('refused')
""")
        assert probe.value == "refused"

    def test_a_probe_resolves_state_inside_the_root(self, tmp_path):
        chaos = ChaosRoot(tmp_path / "c")
        res = run_probe(chaos, "from axiom.infra.paths import get_user_state_dir\nemit(str(get_user_state_dir()))")
        assert res.outcome == "ok"
        assert Path(res.value).resolve() == chaos.state.resolve()


class TestTheDamageLibrary:
    @pytest.fixture
    def target(self, tmp_path):
        root = tmp_path / "r"
        root.mkdir()
        f = root / "state.json"
        f.write_text('{"a": 1, "b": [1, 2, 3], "version": 1}')
        return root, f

    def test_delete(self, target):
        root, f = target
        damage("delete_file", f, root)
        assert not f.exists()

    def test_rename_away_keeps_the_bytes(self, target):
        root, f = target
        damage("rename_away", f, root)
        assert not f.exists() and (root / "state.json.renamed-away").exists()

    def test_truncate_half_is_half(self, target):
        root, f = target
        n = len(f.read_bytes())
        damage("truncate_half", f, root)
        assert 0 < len(f.read_bytes()) <= n // 2 + 1

    def test_zero_length(self, target):
        root, f = target
        damage("zero_length", f, root)
        assert f.read_bytes() == b""

    def test_garbage_is_not_utf8_json(self, target):
        root, f = target
        damage("garbage_bytes", f, root)
        with pytest.raises(ValueError):
            import json

            json.loads(f.read_text(encoding="utf-8"))

    def test_wrong_schema_is_valid_json(self, target):
        import json

        root, f = target
        damage("wrong_schema", f, root)
        assert isinstance(json.loads(f.read_text()), str)

    def test_future_version_keeps_the_content(self, target):
        import json

        root, f = target
        damage("future_version", f, root)
        doc = json.loads(f.read_text())
        assert doc["version"] == 999 and doc["a"] == 1

    def test_permission_000(self, target):
        root, f = target
        damage("perm_000", f, root)
        try:
            assert (f.stat().st_mode & 0o777) == 0
        finally:
            os.chmod(f, 0o600)

    def test_file_to_dir_and_dir_to_file(self, target):
        root, f = target
        damage("file_to_dir", f, root)
        assert f.is_dir()
        damage("dir_to_file", f, root)
        assert f.is_file()

    def test_dangling_symlink(self, target):
        root, f = target
        damage("dangling_symlink", f, root)
        assert f.is_symlink() and not f.exists()

    def test_partial_write_leaves_temp_files_and_the_original(self, target):
        root, f = target
        before = f.read_bytes()
        damage("partial_write", f, root)
        assert f.read_bytes() == before
        assert any(p.name.endswith(".tmp") for p in root.iterdir())

    def test_every_listed_kind_is_implemented(self, tmp_path):
        root = tmp_path / "r"
        root.mkdir()
        for kind in FILE_DAMAGE:
            f = root / f"{kind}.json"
            f.write_text('{"x": 1}')
            damage(kind, f, root)
        for kind in DIR_DAMAGE:
            d = root / f"{kind}.d"
            d.mkdir()
            (d / "inner").write_text("x")
            damage(kind, d, root)
        from .harness import restore_permissions

        restore_permissions(root)


class TestTheClassifier:
    def test_a_named_error_with_a_fix_is_degraded_loudly(self, tmp_path):
        from .harness import ProbeResult

        r = ProbeResult(outcome="error", exc_type="X", exc_msg=f"{tmp_path}/a.json is damaged; restore it")
        assert classify(r, tmp_path / "a.json")[0] == "degraded-loudly"

    def test_a_bare_traceback_class_is_a_crash(self, tmp_path):
        from .harness import ProbeResult

        r = ProbeResult(outcome="error", exc_type="JSONDecodeError", exc_msg="Expecting value: line 1")
        assert classify(r, tmp_path / "a.json")[0] == "crash"

    def test_a_hang_is_a_crash(self, tmp_path):
        from .harness import ProbeResult

        assert classify(ProbeResult(outcome="timeout"), tmp_path / "a")[0] == "crash"

    def test_success_with_a_warning_naming_the_file_is_recovered(self, tmp_path):
        from .harness import ProbeResult

        r = ProbeResult(outcome="ok", log=f"warning: {tmp_path}/a.json quarantined")
        assert classify(r, tmp_path / "a.json")[0] == "recovered"

    def test_success_with_silence_is_silent_default(self, tmp_path):
        from .harness import ProbeResult

        assert classify(ProbeResult(outcome="ok"), tmp_path / "a.json")[0] == "silent-default"

    def test_a_real_hang_is_detected_by_timeout(self, tmp_path):
        chaos = ChaosRoot(tmp_path / "c")
        res = run_probe(chaos, "import time\ntime.sleep(30)", timeout=1.5)
        assert res.outcome == "timeout"
        assert classify(res, chaos.state / "x")[0] == "crash"


def test_the_catalog_has_unique_ids_and_state_relative_paths():
    ids = [a.id for a in ARTIFACTS]
    assert len(ids) == len(set(ids))
    for a in ARTIFACTS:
        assert not Path(a.rel).is_absolute() and ".." not in Path(a.rel).parts
