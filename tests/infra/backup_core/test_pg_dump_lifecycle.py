# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The pg_dump path under the shared lifecycle: verify, listing, retention, lock, preflight, latest.

pg_dump itself is not rewritten and no real database is touched.  Where the command line matters
a stub ``pg_dump`` / ``psql`` / ``pg_restore`` (tiny scripts on a temp PATH) stands in for the
external binaries; one round trip uses a throwaway database and skips when ``pg_dump`` is absent.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.infra import backup as pg
from axiom.infra import backup_core as bc

DUMP_BODY = "-- PostgreSQL database dump\nCREATE TABLE t (a int);\n-- PostgreSQL database dump complete\n"


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!{sys.executable}\nimport os, sys\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def stubs(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    _stub(bindir, "pg_dump", f"""
mode = os.environ.get("STUB_PG_DUMP", "ok")
out = sys.argv[sys.argv.index("-f") + 1]
body = {DUMP_BODY!r}
if mode == "ok":
    open(out, "w").write(body)
elif mode == "partial":
    open(out, "w").write(body[:30]); sys.stderr.write("connection lost"); sys.exit(1)
elif mode == "silent-truncate":
    open(out, "w").write(body[:30])
""")
    _stub(bindir, "psql", f"open({str(log)!r}, 'a').write('psql ' + ' '.join(sys.argv[1:]) + '\\n')")
    _stub(bindir, "pg_restore", f"open({str(log)!r}, 'a').write('pg_restore ' + ' '.join(sys.argv[1:]) + '\\n')")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


@pytest.fixture
def bdir(tmp_path):
    d = tmp_path / "pgbackups"
    d.mkdir()
    return d


def _make(bdir, n=1, **kw):
    out = []
    for i in range(n):
        r = pg.create_backup("postgresql://stub/none", output_dir=bdir, label=f"n{i}", **kw)
        assert r.success, r.error
        out.append(r.backup_path)
    return out


def test_a_good_dump_gets_a_manifest_and_verifies(stubs, bdir):
    (path,) = _make(bdir)
    assert (bdir / ("." + path.name + ".manifest.json")).exists()
    assert pg.verify_backup(path) == []


def test_a_failed_dump_leaves_no_partial_file_to_be_listed_as_a_backup(stubs, bdir, monkeypatch):
    monkeypatch.setenv("STUB_PG_DUMP", "partial")
    r = pg.create_backup("postgresql://stub/none", output_dir=bdir)
    assert not r.success and "connection lost" in r.error
    assert pg.list_backups(bdir) == [] and not any(bdir.glob("axiom-backup-*"))


def test_a_dump_that_exits_zero_but_is_truncated_is_rejected_at_write_time(stubs, bdir, monkeypatch):
    monkeypatch.setenv("STUB_PG_DUMP", "silent-truncate")
    r = pg.create_backup("postgresql://stub/none", output_dir=bdir)
    assert not r.success and "no closing line" in r.error
    assert pg.list_backups(bdir) == []


@pytest.mark.parametrize("how", ["truncate", "flip", "zero", "delete"])
def test_verify_names_the_damage(stubs, bdir, how):
    (path,) = _make(bdir)
    data = path.read_bytes()
    if how == "truncate":
        path.write_bytes(data[: len(data) // 2])
    elif how == "flip":
        b = bytearray(data)
        b[5] ^= 0xFF
        path.write_bytes(bytes(b))
    elif how == "zero":
        path.write_bytes(b"")
    else:
        path.unlink()
    assert pg.verify_backup(path)


def test_restore_refuses_a_bad_dump_before_psql_runs(stubs, bdir):
    (path,) = _make(bdir)
    path.write_bytes(path.read_bytes()[:20])
    r = pg.restore_backup(path, "postgresql://stub/none", confirm=True)
    assert not r.success and "REFUSING" in r.error and "database was not touched" in r.error
    assert not stubs.exists()  # psql never ran


def test_custom_format_dumps_restore_with_pg_restore_not_psql(stubs, bdir):
    path = bdir / "axiom-backup-20260101_000000.dump"
    path.write_bytes(b"PGDMP dry run backup\n")
    assert pg.restore_backup(path, "postgresql://stub/none", confirm=True).success
    assert stubs.read_text().startswith("pg_restore ")


def _age(path: Path, hours: float) -> None:
    t = (datetime.now(UTC) - timedelta(hours=hours)).timestamp()
    os.utime(path, (t, t))


def test_restore_latest_falls_back_to_the_newest_dump_that_verifies_and_says_so(stubs, bdir):
    a, b, c = _make(bdir, 3)
    for i, p in enumerate((a, b, c)):
        _age(p, 10 - i)
    c.write_bytes(c.read_bytes()[:25])  # newest truncated
    b.write_bytes(b"")  # next newest empty
    out = pg.restore_latest(bdir, "postgresql://stub/none", confirm=True)
    assert out.ok and out.selection.chosen == a.name and out.selection.fell_back
    text = "\n".join(out.notes)
    assert c.name in text and b.name in text and "using the newest database backup that verifies" in text
    assert a.name in stubs.read_text()


def test_restore_latest_with_every_dump_bad_refuses_and_lists_each_reason(stubs, bdir):
    a, b = _make(bdir, 2)
    for p in (a, b):
        p.write_bytes(b"")
    out = pg.restore_latest(bdir, "postgresql://stub/none", confirm=True)
    assert not out.ok and out.result is None
    assert a.name in out.error and b.name in out.error and "not touched" in out.error and "second copy" in out.error
    assert not stubs.exists()


def test_restore_latest_requires_confirmation(stubs, bdir):
    with pytest.raises(ValueError):
        pg.restore_latest(bdir, "postgresql://stub/none")


def test_prune_keeps_a_verified_dump_even_under_a_policy_that_keeps_nothing(stubs, bdir):
    paths = _make(bdir, 5)
    for i, p in enumerate(paths):
        _age(p, 24 * 60 * (5 - i))
    for p in paths[1:]:
        p.write_bytes(b"")  # four corrupt, only the oldest is intact
    nothing = bc.RetentionPolicy(keep_last=0, keep_recent_hours=0, keep_daily=0, keep_weekly=0,
                                 keep_monthly=0, protect_newest=1, max_count=1)
    pg.prune_backups(bdir, nothing, apply=True)
    left = [b.backup_path for b in pg.list_backups(bdir)]
    assert paths[0] in left and pg.verify_backup(paths[0]) == []


def test_prune_dry_run_changes_nothing(stubs, bdir):
    _make(bdir, 3)
    before = sorted(p.name for p in bdir.iterdir())
    pg.prune_backups(bdir, bc.RetentionPolicy(keep_last=1, keep_recent_hours=0, keep_daily=0, keep_weekly=0,
                                              keep_monthly=0, protect_newest=1))
    assert sorted(p.name for p in bdir.iterdir()) == before


def test_low_disk_refuses_before_writing(stubs, bdir, monkeypatch):
    monkeypatch.setattr(bc, "free_bytes", lambda p: (1024, 10**12))
    r = pg.create_backup("postgresql://stub/none", output_dir=bdir)
    assert not r.success and "not enough free space" in r.error and "nothing was written" in r.error
    assert not any(bdir.glob("axiom-backup-*"))


def test_a_second_backup_reports_busy_while_the_directory_lock_is_held(stubs, bdir, monkeypatch):
    held, release = threading.Event(), threading.Event()
    real = bc.backup_lock

    def holder():
        with real(bdir / ".lock", busy=RuntimeError):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    monkeypatch.setattr(bc, "backup_lock", lambda lockfile, **kw: real(lockfile, **{**kw, "timeout": 0.2}))
    try:
        with pytest.raises(pg.BackupBusy, match="another backup or prune is running"):
            pg.create_backup("postgresql://stub/none", output_dir=bdir)
        with pytest.raises(pg.BackupBusy):
            pg.prune_backups(bdir, apply=True)
    finally:
        release.set()
        t.join()
    assert not any(bdir.glob("axiom-backup-*"))


def test_pg_kind_satisfies_the_backup_kind_protocol(bdir):
    assert isinstance(pg.PgDumpKind(bdir), bc.BackupKind)


def test_round_trip_against_a_throwaway_database(request, tmp_path):
    import shutil

    if not shutil.which("pg_dump") or not shutil.which("psql"):
        pytest.skip("pg_dump/psql are not installed on this machine; the stubbed lifecycle tests above "
                    "cover everything except the real round trip")
    url = request.getfixturevalue("real_postgres")
    out = tmp_path / "rt"
    r = pg.create_backup(url, output_dir=out, fmt="plain", schemas=["public"])
    assert r.success, r.error
    assert pg.verify_backup(r.backup_path) == []
