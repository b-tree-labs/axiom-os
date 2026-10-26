# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""State snapshots: capture, integrity, restore safety, retention, seams.

Every test runs against a throwaway tree.  ``_guard`` asserts that before
anything is written, so a mistake in a fixture fails loudly instead of touching
a real ``~/.axi``.
"""

from __future__ import annotations

import gzip
import json
import os
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.infra import backup_core as bc
from axiom.infra import state_snapshots as ss
from axiom.infra.state_snapshots import (
    RetentionPolicy,
    SnapshotInfo,
    SnapshotPolicy,
    plan_retention,
)

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "axi-restore.py"


def _guard(*paths: Path) -> None:
    real = Path.home().resolve() / ".axi"
    tmp_roots = {Path(os.environ["AXIOM_TEST_TMP_ROOT"]).resolve()}
    for p in paths:
        rp = p.resolve()
        assert real not in (rp, *rp.parents), f"{p} is inside the real state dir"
        assert any(t in (rp, *rp.parents) for t in tmp_roots), f"{p} is not under the temp root"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    monkeypatch.setenv("AXIOM_TEST_TMP_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.setenv("AXIOM_STATE_DIR", str(state))
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    _guard(home, state)
    return state


def _populate(state: Path) -> None:
    files = {
        "identity/identity.json": '{"owner": "someone"}',
        "settings.toml": 'name = "x"\n',
        "orchestrator/approvals.json": '{"pending": [1, 2, 3]}',
        "sessions/s1.json": '{"turns": 4}',
        "agents/commands/commands_state.json": '{"c": 1}',
        "memory/audit.jsonl": '{"a":1}\n{"a":2}\n',
        "gate/users.json": "{}",
    }
    for rel, text in files.items():
        p = state / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    os.chmod(state / "identity/identity.json", 0o600)


def _tree(state: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(state.rglob("*")):
        rel = p.relative_to(state).as_posix()
        if (p.is_file() and not rel.startswith("snapshots/") and ".pre-restore-" not in rel
                and rel != "secrets/snapshot-head.json"):  # store bookkeeping, excluded from snapshots
            out[rel] = p.read_bytes()
    return out


def _snap(state: Path, root: Path | None = None, **kw):
    kw.setdefault("policy", SnapshotPolicy())
    return ss.create_snapshot(state, root, **kw)


# ===========================================================================
# plan_retention -- pure
# ===========================================================================

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)


def _info(i, hours_ago, *, size=10, verified=True, pinned=False, at=None):
    return SnapshotInfo(
        id=f"s{i:03d}", created_at=at or (NOW - timedelta(hours=hours_ago)),
        size_bytes=size, verified=verified, pinned=pinned,
    )


def _kept(plan):
    return {d.id for d in plan.keep}


def test_empty_input_is_an_empty_plan():
    assert plan_retention([], RetentionPolicy(), NOW).decisions == ()


def test_everything_in_the_recent_window_is_kept():
    snaps = [_info(i, i) for i in range(1, 12)]  # 11 snapshots in 11h
    plan = plan_retention(snaps, RetentionPolicy(keep_last=0), NOW)
    assert _kept(plan) == {s.id for s in snaps}


def test_gfs_thins_old_history_one_per_day():
    # 4 per day for 30 days
    snaps = [_info(i, h) for i, h in enumerate(range(1, 24 * 30, 6), 1)]
    pol = RetentionPolicy(keep_last=0, keep_recent_hours=24, keep_daily=5, keep_weekly=0,
                          keep_monthly=0, protect_newest=1, max_count=0, max_bytes=0)
    plan = plan_retention(snaps, pol, NOW)
    kept = [d for d in plan.keep]
    daily = [d for d in kept if any(r.startswith("daily:") for r in d.reasons)]
    assert len(daily) == 5
    days = {r for d in daily for r in d.reasons if r.startswith("daily:")}
    assert len(days) == 5
    # nothing older than the 5th day survives
    oldest_kept = min(snaps, key=lambda s: s.created_at if s.id in _kept(plan) else NOW + timedelta(days=1))
    assert (NOW - oldest_kept.created_at) < timedelta(days=7)
    assert len(plan.delete) > 0


def test_weekly_and_monthly_tiers_reach_back():
    snaps = [_info(i, 24 * d) for i, d in enumerate(range(1, 200, 3), 1)]
    pol = RetentionPolicy(keep_last=0, keep_recent_hours=0, keep_daily=0, keep_weekly=4,
                          keep_monthly=3, protect_newest=1, max_count=0, max_bytes=0)
    plan = plan_retention(snaps, pol, NOW)
    reasons = {r.split(":")[0] for d in plan.keep for r in d.reasons}
    assert {"weekly", "monthly"} <= reasons
    assert sum(1 for d in plan.keep if any(r.startswith("monthly:") for r in d.reasons)) == 3


def test_newest_n_are_never_pruned_even_under_zero_policy():
    snaps = [_info(i, 24 * 400 - i) for i in range(1, 10)]  # all ancient
    pol = RetentionPolicy(keep_last=0, keep_recent_hours=0, keep_daily=0, keep_weekly=0,
                          keep_monthly=0, protect_newest=3, max_count=1, max_bytes=1)
    plan = plan_retention(snaps, pol, NOW)
    newest3 = {s.id for s in sorted(snaps, key=lambda s: s.created_at)[-3:]}
    assert newest3 <= _kept(plan)
    assert plan.over_budget  # caps cannot be met without breaking the guard, and it says so


def test_protect_newest_cannot_be_configured_to_zero():
    snaps = [_info(1, 24 * 400), _info(2, 24 * 401)]
    pol = RetentionPolicy(keep_last=0, keep_recent_hours=0, keep_daily=0, keep_weekly=0,
                          keep_monthly=0, protect_newest=0)
    assert _kept(plan_retention(snaps, pol, NOW)) >= {"s001"}


def test_pinned_snapshot_is_never_pruned():
    snaps = [_info(1, 24 * 300, pinned=True)] + [_info(i, i) for i in range(2, 30)]
    pol = RetentionPolicy(max_count=5, max_bytes=0)
    assert "s001" in _kept(plan_retention(snaps, pol, NOW))


def test_newest_verified_snapshot_is_kept_when_newer_ones_are_corrupt():
    snaps = [_info(i, i, verified=False) for i in range(1, 8)] + [_info(99, 24 * 90, verified=True)]
    pol = RetentionPolicy(keep_last=0, keep_recent_hours=0, keep_daily=0, keep_weekly=0,
                          keep_monthly=0, protect_newest=1)
    plan = plan_retention(snaps, pol, NOW)
    assert "s099" in _kept(plan)
    assert "newest-verified" in plan.reasons_for("s099")


def test_all_corrupt_prunes_nothing():
    snaps = [_info(i, 24 * 100 * i, verified=False) for i in range(1, 8)]
    plan = plan_retention(snaps, RetentionPolicy(keep_last=0, keep_recent_hours=0, keep_daily=0,
                                                  keep_weekly=0, keep_monthly=0), NOW)
    assert plan.delete == []
    assert any("no snapshot is verified" in w for w in plan.warnings)


def test_all_unknown_verification_prunes_nothing():
    snaps = [_info(i, 24 * 100 * i, verified=None) for i in range(1, 5)]
    assert plan_retention(snaps, RetentionPolicy(keep_last=0, keep_recent_hours=0), NOW).delete == []


def test_pruning_never_leaves_zero_verified():
    # only verified snapshot is ancient; newest 3 are corrupt and protected
    snaps = [_info(1, 1, verified=False), _info(2, 2, verified=False), _info(3, 3, verified=False),
             _info(4, 24 * 500, verified=True)]
    pol = RetentionPolicy(keep_last=0, keep_recent_hours=0, keep_daily=0, keep_weekly=0,
                          keep_monthly=0, protect_newest=3)
    plan = plan_retention(snaps, pol, NOW)
    assert "s004" in _kept(plan)


def test_corrupt_unprotected_snapshots_are_pruned_when_a_good_one_exists():
    snaps = [_info(i, i) for i in range(1, 5)] + [_info(9, 30, verified=False)]
    plan = plan_retention(snaps, RetentionPolicy(keep_last=2, keep_recent_hours=0, keep_daily=0,
                                                  keep_weekly=0, keep_monthly=0), NOW)
    assert "s009" in {d.id for d in plan.delete}


def test_equal_timestamps_are_deterministic_and_do_not_crash():
    snaps = [_info(i, 5, at=NOW - timedelta(hours=5)) for i in range(1, 8)]
    pol = RetentionPolicy(keep_last=2, keep_recent_hours=0, keep_daily=1, keep_weekly=0,
                          keep_monthly=0, protect_newest=1, max_count=0, max_bytes=0)
    a = plan_retention(snaps, pol, NOW)
    b = plan_retention(list(reversed(snaps)), pol, NOW)
    assert a.decisions == b.decisions
    assert "s007" in _kept(a)  # tie broken by id: highest id counts as newest


def test_future_dated_snapshot_does_not_displace_real_history():
    future = _info(50, 0, at=NOW + timedelta(days=400))
    old = [_info(i, 24 * i) for i in range(1, 30)]
    pol = RetentionPolicy(keep_last=0, keep_recent_hours=0, keep_daily=3, keep_weekly=0,
                          keep_monthly=0, protect_newest=1, max_count=0, max_bytes=0)
    plan = plan_retention([future, *old], pol, NOW)
    assert any("future" in w for w in plan.warnings)
    # the clamp means the future-dated one is bucketed as TODAY, not as a day 400 days out
    assert "daily:2026-10-08" in plan.reasons_for("s050")
    assert not any(r.startswith("daily:2027") for d in plan.keep for r in d.reasons)
    day_buckets = {r for d in plan.keep for r in d.reasons if r.startswith("daily:")}
    assert len(day_buckets) == 3


def test_count_cap_evicts_coarsest_oldest_first_and_keeps_recent():
    snaps = [_info(i, i) for i in range(1, 11)] + [_info(100 + i, 24 * 40 * i) for i in range(1, 5)]
    pol = RetentionPolicy(keep_last=0, keep_recent_hours=24, keep_daily=0, keep_weekly=0,
                          keep_monthly=6, protect_newest=1, max_count=10, max_bytes=0)
    plan = plan_retention(snaps, pol, NOW)
    kept = _kept(plan)
    assert len(kept) == 10
    assert all(f"s{i:03d}" in kept for i in range(1, 11))  # recent ones survive
    assert not {"s101", "s102", "s103", "s104"} & kept  # monthly ones went


def test_byte_cap_uses_dedup_aware_union():
    shared = {"sha-shared": 1000}
    snaps = [
        SnapshotInfo(f"s{i}", NOW - timedelta(hours=i), size_bytes=1000, verified=True,
                     blobs={**shared, f"own-{i}": 10})
        for i in range(1, 6)
    ]
    pol = RetentionPolicy(keep_last=10, max_bytes=1100, max_count=0, protect_newest=1)
    plan = plan_retention(snaps, pol, NOW)
    # true cost of keeping all five is 1000 + 50 = 1050 <= 1100: nothing evicted
    assert len(plan.delete) == 0
    pol2 = RetentionPolicy(keep_last=10, max_bytes=1030, max_count=0, protect_newest=1)
    assert len(plan_retention(snaps, pol2, NOW).delete) >= 2


def test_pressure_prunes_harder_than_normal():
    snaps = [_info(i, 3 * i) for i in range(1, 60)]
    normal = plan_retention(snaps, RetentionPolicy(), NOW)
    hard = plan_retention(snaps, RetentionPolicy(), NOW, pressure=True)
    assert len(hard.keep) < len(normal.keep)
    assert {"s001", "s002", "s003"} <= _kept(hard)  # the guard still holds


def test_negative_policy_values_are_clamped_not_trusted():
    snaps = [_info(i, i) for i in range(1, 5)]
    plan = plan_retention(snaps, RetentionPolicy(keep_last=-5, keep_daily=-1, protect_newest=-2), NOW)
    assert _kept(plan)  # no crash, something kept


def test_plan_is_pure_inputs_unchanged():
    snaps = [_info(i, i) for i in range(1, 6)]
    before = list(snaps)
    plan_retention(snaps, RetentionPolicy(), NOW)
    assert snaps == before


def test_default_policy_numbers_are_the_documented_ones():
    p = RetentionPolicy()
    assert (p.keep_last, p.keep_recent_hours, p.keep_daily, p.keep_weekly, p.keep_monthly) == (24, 24, 14, 8, 6)
    assert (p.protect_newest, p.max_count, p.max_bytes) == (3, 100, 2 * 1024**3)
    assert SnapshotPolicy().interval_hours == 6


def test_policy_from_settings_and_invalid_values():
    pol = ss.policy_from_settings({
        "state.snapshots.keep_last": 5, "state.snapshots.interval_hours": "2",
        "state.snapshots.keep_daily": "bogus", "state.snapshots.exclude": ["a/**"],
        "state.snapshots.enabled": "off",
    })
    assert pol.retention.keep_last == 5 and pol.interval_hours == 2.0
    assert pol.retention.keep_daily == 14  # invalid -> default, not a crash
    assert pol.extra_exclude == ("a/**",) and pol.enabled is False


def test_load_policy_reads_settings_toml(env):
    (env / "settings.toml").write_text("[state.snapshots]\nkeep_last = 7\nmax_count = 9\n")
    pol = ss.load_policy(env)
    assert pol.retention.keep_last == 7 and pol.retention.max_count == 9


# ===========================================================================
# create
# ===========================================================================


def test_snapshot_captures_state_with_tiers_and_metadata(env):
    _populate(env)
    r = _snap(env)
    assert r.ok and r.snapshot_id and r.files == 7
    store = ss.open_store(env)
    m = store.read_manifest(r.snapshot_id)["body"]
    by = {f["path"]: f for f in m["files"]}
    assert by["identity/identity.json"]["tier"] == "critical"
    assert by["orchestrator/approvals.json"]["tier"] == "critical"
    assert by["sessions/s1.json"]["tier"] == "useful"
    assert by["identity/identity.json"]["mode"] == 0o600
    assert len(by["settings.toml"]["sha256"]) == 64 and by["settings.toml"]["size"] > 0
    for k in ("version", "created_at", "host", "axiom_version", "reason", "excluded", "db", "not_covered"):
        assert k in m
    assert m["db"]["covered"] is False and "state snapshot --with-db" in m["db"]["how"]


def test_nothing_changed_means_no_new_snapshot(env):
    _populate(env)
    first = _snap(env)
    second = _snap(env)
    assert second.skipped and second.snapshot_id == first.snapshot_id
    assert len(ss.open_store(env).ids()) == 1


def test_a_change_makes_a_new_snapshot_and_force_overrides_skip(env):
    _populate(env)
    a = _snap(env)
    (env / "settings.toml").write_text('name = "y"\n')
    b = _snap(env)
    assert not b.skipped and b.snapshot_id != a.snapshot_id
    c = _snap(env, force=True)
    assert not c.skipped and c.snapshot_id not in (a.snapshot_id, b.snapshot_id)


def test_growth_confined_to_churn_files_does_not_force_a_snapshot(env):
    _populate(env)
    (env / "audit").mkdir()
    (env / "audit" / "actions.jsonl").write_text("1\n")
    a = _snap(env)
    (env / "audit" / "actions.jsonl").write_text("1\n2\n")
    b = _snap(env)
    assert b.skipped and b.snapshot_id == a.snapshot_id
    # ...but a real change still does, and then carries the newer audit file along
    (env / "settings.toml").write_text("new")
    c = _snap(env)
    assert not c.skipped
    store = ss.open_store(env)
    f = next(x for x in store.read_manifest(c.snapshot_id)["body"]["files"] if x["path"] == "audit/actions.jsonl")
    assert store.read_blob(f["sha256"]) == b"1\n2\n"


def test_critical_files_are_not_dropped_for_size_but_useful_ones_are(env):
    _populate(env)
    (env / "memory" / "artifacts.db").write_bytes(b"x" * 5000)       # critical, over the 1000 limit
    (env / "sessions" / "huge.json").write_bytes(b"y" * 5000)        # useful, over the limit
    r = _snap(env, policy=SnapshotPolicy(max_file_bytes=1000))
    m = ss.open_store(env).read_manifest(r.snapshot_id)["body"]
    paths = {f["path"] for f in m["files"]}
    assert "memory/artifacts.db" in paths and "sessions/huge.json" not in paths
    ex = next(e for e in m["excluded"] if "max_file_bytes" in e["reason"])
    assert ex["examples"] == ["sessions/huge.json"]


def test_mode_change_alone_counts_as_a_change(env):
    _populate(env)
    _snap(env)
    os.chmod(env / "settings.toml", 0o600)
    assert not _snap(env).skipped


def test_secret_values_never_enter_the_snapshot(env):
    _populate(env)
    sentinel = b"SENTINEL-credential-value-9f3a"
    (env / "credentials").mkdir()
    (env / "credentials" / "gitlab").write_bytes(sentinel)
    (env / "secrets").mkdir()
    (env / "secrets" / "foreign-values.json").write_bytes(b'{"k": "' + sentinel + b'"}')
    (env / "secrets" / "foreign-credentials.json").write_text('{"names": ["gitlab"]}')
    (env / "local").mkdir()
    (env / "local" / "gate-oidc.secret").write_bytes(sentinel)
    (env / "fleet-token-x.txt").write_bytes(sentinel)
    r = _snap(env)
    store = ss.open_store(env)
    # scan every stored byte, decompressed, and the manifests as text
    haystack = b""
    for p in store.blobs.rglob("*"):
        if p.is_file():
            haystack += gzip.open(p, "rb").read()
    for p in store.snaps.rglob("manifest.json"):
        haystack += p.read_bytes()
    assert sentinel not in haystack
    m = store.read_manifest(r.snapshot_id)["body"]
    assert "credentials/gitlab" in m["secret_metadata"]  # names yes, values no
    paths = {f["path"] for f in m["files"]}
    assert "secrets/foreign-credentials.json" in paths  # the METADATA index is kept
    assert "credentials/gitlab" not in paths


def test_caches_logs_venvs_and_volatiles_are_excluded_with_reasons(env):
    _populate(env)
    for rel in ("service-venv/bin/python", "logs/a.log", "x/__pycache__/m.pyc", "cache/c", "run.pid",
                "locks/l.lock", "memory/recall.db", "services/x.stderr.log"):
        p = env / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    r = _snap(env)
    m = ss.open_store(env).read_manifest(r.snapshot_id)["body"]
    paths = {f["path"] for f in m["files"]}
    assert not any(p.startswith(("service-venv", "logs", "cache", "locks")) or p.endswith((".pid", ".log", ".pyc")) for p in paths)
    assert "memory/recall.db" not in paths
    reasons = " ".join(e["reason"] for e in m["excluded"])
    assert "python environment" in reasons and "log output" in reasons
    assert any(e["dir"] == "service-venv" for e in m["excluded"] if "dir" in e)


def test_snapshot_dir_is_never_captured_into_itself(env):
    _populate(env)
    _snap(env)
    (env / "settings.toml").write_text("changed")
    r = _snap(env)
    m = ss.open_store(env).read_manifest(r.snapshot_id)["body"]
    assert not any(f["path"].startswith("snapshots/") for f in m["files"])


def test_user_exclude_globs_win(env):
    _populate(env)
    r = _snap(env, policy=SnapshotPolicy(extra_exclude=("sessions/**",)))
    m = ss.open_store(env).read_manifest(r.snapshot_id)["body"]
    assert not any(f["path"].startswith("sessions/") for f in m["files"])


def test_oversize_files_are_excluded_by_declared_limit(env):
    _populate(env)
    (env / "big.bin").write_bytes(b"x" * 5000)
    r = _snap(env, policy=SnapshotPolicy(max_file_bytes=1000))
    m = ss.open_store(env).read_manifest(r.snapshot_id)["body"]
    assert "big.bin" not in {f["path"] for f in m["files"]}
    assert any("max_file_bytes" in e["reason"] for e in m["excluded"])


def test_symlinks_are_not_followed(env, tmp_path):
    _populate(env)
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE")
    os.symlink(outside, env / "link.txt")
    r = _snap(env)
    m = ss.open_store(env).read_manifest(r.snapshot_id)["body"]
    assert "link.txt" not in {f["path"] for f in m["files"]}


def test_blobs_dedupe_across_snapshots(env):
    _populate(env)
    _snap(env)
    store = ss.open_store(env)
    n1 = sum(1 for p in store.blobs.rglob("*") if p.is_file())
    (env / "settings.toml").write_text("changed")
    _snap(env)
    n2 = sum(1 for p in store.blobs.rglob("*") if p.is_file())
    assert n2 == n1 + 1  # one new blob, six shared


def test_store_is_owner_only(env):
    _populate(env)
    r = _snap(env)
    store = ss.open_store(env)
    for p in [store.root, store.blobs, store.snaps, *store.snaps.iterdir()]:
        assert stat.S_IMODE(p.stat().st_mode) == 0o700, p
    for p in [*store.blobs.rglob("*"), store.ledger, store.snap_dir(r.snapshot_id) / "manifest.json"]:
        if p.is_file():
            assert stat.S_IMODE(p.stat().st_mode) == 0o600, p


def test_live_sqlite_database_is_captured_consistently(env):
    import sqlite3

    _populate(env)
    db = env / "memory" / "artifacts.db"
    con = sqlite3.connect(db)
    con.execute("create table t (v text)")
    con.executemany("insert into t values (?)", [(str(i),) for i in range(200)])
    con.commit()
    # leave a WAL-mode writer open
    con.execute("pragma journal_mode=wal")
    con.execute("insert into t values ('uncheckpointed')")
    con.commit()
    r = _snap(env)
    store = ss.open_store(env)
    f = next(x for x in store.read_manifest(r.snapshot_id)["body"]["files"] if x["path"] == "memory/artifacts.db")
    out = env.parent / "copy.db"
    out.write_bytes(store.read_blob(f["sha256"]))
    copy = sqlite3.connect(out)
    assert copy.execute("select count(*) from t").fetchone()[0] == 201
    con.close()
    copy.close()


def test_creation_is_atomic_a_crash_leaves_no_visible_snapshot(env, monkeypatch):
    _populate(env)
    real_rename = os.rename

    def boom(src, dst):
        if "snaps" in str(dst):
            raise OSError("simulated crash at publish")
        return real_rename(src, dst)

    with monkeypatch.context() as scoped:  # scoped: undo() would also revert the fixtures' settings
        scoped.setattr(os, "rename", boom)
        with pytest.raises(ss.SnapshotError, match="nothing was published"):
            _snap(env)
        store = ss.open_store(env)
        assert store.ids() == []
        assert not any(store.snaps.iterdir())
    assert _snap(env).ok  # and the store is usable afterwards


def test_same_device_is_warned_about(env):
    _populate(env)
    r = _snap(env)
    assert any("same device" in w for w in r.warnings)


def test_snapshot_to_another_directory(env, tmp_path):
    _populate(env)
    dest = tmp_path / "elsewhere" / "snaps"
    r = _snap(env, dest)
    assert r.ok and (dest / "snaps" / r.snapshot_id / "manifest.json").is_file()
    assert not (env / "snapshots").exists()


def test_concurrent_writer_is_refused_not_corrupted(env):
    _populate(env)
    store = ss.open_store(env)
    with store.locked():
        with pytest.raises(ss.SnapshotBusy):
            with ss.Store(store.root).locked(timeout=0.2):
                pass


# ===========================================================================
# disk pressure
# ===========================================================================


def test_low_disk_refuses_loudly_instead_of_filling_it(env, monkeypatch):
    _populate(env)
    monkeypatch.setattr(bc, "free_bytes", lambda p: (10, 10**12))
    with pytest.raises(ss.DiskPressureError) as ei:
        _snap(env)
    assert "REFUSING" in str(ei.value)
    assert ss.open_store(env).ids() == []


def test_low_disk_prunes_harder_then_succeeds(env, monkeypatch):
    _populate(env)
    pol = SnapshotPolicy(retention=RetentionPolicy(keep_last=12, keep_recent_hours=0, keep_daily=0,
                                                    keep_weekly=0, keep_monthly=0, max_count=0, max_bytes=0))
    for i in range(8):
        (env / "settings.toml").write_text(f"v{i}")
        _snap(env, policy=pol, prune=False)
    store = ss.open_store(env)
    assert len(store.ids()) == 8
    calls = {"n": 0}

    def free(_p):
        calls["n"] += 1
        return (10, 10**12) if calls["n"] == 1 else (10**11, 10**12)

    monkeypatch.setattr(bc, "free_bytes", free)
    (env / "settings.toml").write_text("final")
    r = _snap(env, policy=pol)
    assert r.ok and r.pruned and any("low disk" in w for w in r.warnings)
    assert len(store.ids()) < 9


# ===========================================================================
# verify
# ===========================================================================


def _only_blob(store, rel_path, snap_id):
    m = store.read_manifest(snap_id)
    return next(f["sha256"] for f in m["body"]["files"] if f["path"] == rel_path)


def test_verify_passes_on_a_fresh_store(env):
    _populate(env)
    _snap(env)
    v = ss.verify_store(ss.open_store(env))
    assert v.ok and all(r.ok for r in v.results) and v.ledger.ok


def test_verify_detects_bitrot_in_a_blob(env):
    _populate(env)
    r = _snap(env)
    store = ss.open_store(env)
    sha = _only_blob(store, "settings.toml", r.snapshot_id)
    p = store.blob_path(sha)
    p.write_bytes(gzip.compress(b"rotted"))
    res = ss.verify_snapshot(store, r.snapshot_id)
    assert not res.ok and any("settings.toml" in x for x in res.problems)
    assert ss.verify_snapshot(store, r.snapshot_id, deep=False).ok  # shallow cannot see content rot


def test_verify_detects_a_missing_blob(env):
    _populate(env)
    r = _snap(env)
    store = ss.open_store(env)
    store.blob_path(_only_blob(store, "settings.toml", r.snapshot_id)).unlink()
    assert not ss.verify_snapshot(store, r.snapshot_id, deep=False).ok


def test_verify_detects_an_edited_manifest(env):
    _populate(env)
    r = _snap(env)
    store = ss.open_store(env)
    mp = store.snap_dir(r.snapshot_id) / "manifest.json"
    m = json.loads(mp.read_text())
    m["body"]["files"][0]["size"] += 1
    mp.write_text(json.dumps(m))
    res = ss.verify_snapshot(store, r.snapshot_id)
    assert not res.ok and any("recorded hash" in x for x in res.problems)


def test_verify_detects_a_rehashed_manifest_via_the_ledger(env):
    """An attacker who recomputes body_sha256/chain_hash is still caught by the ledger."""
    _populate(env)
    r = _snap(env)
    store = ss.open_store(env)
    mp = store.snap_dir(r.snapshot_id) / "manifest.json"
    m = json.loads(mp.read_text())
    m["body"]["reason"] = "forged"
    m["body_sha256"], m["chain_hash"] = bc.manifest_chain(m["prev"], m["body"])
    mp.write_text(json.dumps(m))
    assert ss.verify_snapshot(store, r.snapshot_id).ok  # self-consistent...
    v = ss.verify_store(store)
    assert not v.ok and any("substituted" in p for x in v.results for p in x.problems)


def test_verify_detects_a_broken_ledger_chain(env):
    _populate(env)
    _snap(env)
    (env / "settings.toml").write_text("b")
    _snap(env)
    store = ss.open_store(env)
    lines = store.ledger.read_text().splitlines()
    first = json.loads(lines[0])
    first["detail"]["reason"] = "edited"
    lines[0] = json.dumps(first)
    store.ledger.write_text("\n".join(lines) + "\n")
    assert not ss.verify_ledger(store).ok


def test_verify_detects_a_ledger_line_removed_without_touching_the_others(env):
    _populate(env)
    for i in range(3):
        (env / "settings.toml").write_text(f"v{i}")
        _snap(env, prune=False)
        time.sleep(1.01)
    store = ss.open_store(env)
    lines = store.ledger.read_text().splitlines()
    assert len(lines) == 3
    del lines[1]  # every remaining line still hashes correctly; only the chain breaks
    store.ledger.write_text("\n".join(lines) + "\n")
    chk = ss.verify_ledger(store)
    assert not chk.ok and any("chain does not link" in p for p in chk.problems)


def test_a_snapshot_deleted_outside_the_tool_is_flagged_but_a_pruned_one_is_not(env):
    _populate(env)
    pol = SnapshotPolicy(retention=RetentionPolicy(keep_last=0, keep_recent_hours=0, keep_daily=0,
                                                    keep_weekly=0, keep_monthly=0, protect_newest=1,
                                                    max_count=0, max_bytes=0))
    ids = []
    for i in range(4):
        (env / "settings.toml").write_text(f"v{i}")
        ids.append(_snap(env, policy=pol, prune=False).snapshot_id)
        time.sleep(1.01)
    store = ss.open_store(env)
    ss.prune_store(store, pol, apply=True)  # legitimately removes older ones
    assert ss.verify_store(store).ok  # pruned != missing
    survivors = store.ids()
    import shutil

    shutil.rmtree(store.snap_dir(survivors[0]))  # now delete one behind its back
    v = ss.verify_store(store)
    assert not v.ok and any("deleted outside the tool" in p for p in v.store_problems)


# ===========================================================================
# restore
# ===========================================================================


def test_dry_run_changes_nothing_and_shows_the_diff(env):
    _populate(env)
    r = _snap(env)
    (env / "settings.toml").write_text("broken")
    (env / "sessions" / "s1.json").unlink()
    before = _tree(env)
    res = ss.restore_snapshot(env, None, r.snapshot_id)
    assert res.dry_run and not res.applied
    assert {d.path: d.action for d in res.diff if d.action != "unchanged"} == {
        "settings.toml": "replace", "sessions/s1.json": "create"}
    assert _tree(env) == before
    assert not list(env.rglob("*.pre-restore-*"))


def test_restore_returns_state_to_the_snapshot_and_keeps_what_it_replaced(env):
    _populate(env)
    original = _tree(env)
    r = _snap(env)
    (env / "settings.toml").write_text("broken")
    (env / "identity" / "identity.json").unlink()
    res = ss.restore_snapshot(env, None, r.snapshot_id, apply=True, _services=lambda _s: [])
    assert res.ok and res.applied
    assert _tree(env) == original
    aside = Path(res.set_aside["settings.toml"])
    assert aside.read_text() == "broken"  # preserved, not overwritten
    assert ".pre-restore-" in aside.name
    assert stat.S_IMODE((env / "identity" / "identity.json").stat().st_mode) == 0o600


def test_restore_takes_a_pre_restore_snapshot_that_can_undo_it(env):
    _populate(env)
    r = _snap(env)
    (env / "settings.toml").write_text("my newer work")
    res = ss.restore_snapshot(env, None, r.snapshot_id, apply=True, _services=lambda _s: [])
    assert res.pre_restore_snapshot
    assert (env / "settings.toml").read_text() == 'name = "x"\n'
    undo = ss.restore_snapshot(env, None, res.pre_restore_snapshot, apply=True, _services=lambda _s: [])
    assert undo.ok and (env / "settings.toml").read_text() == "my newer work"


def test_restore_never_deletes_files_the_snapshot_does_not_have(env):
    _populate(env)
    r = _snap(env)
    (env / "newer.json").write_text("created after the snapshot")
    ss.restore_snapshot(env, None, r.snapshot_id, apply=True, _services=lambda _s: [])
    assert (env / "newer.json").read_text() == "created after the snapshot"


def test_restore_refuses_a_snapshot_that_fails_verify(env):
    _populate(env)
    r = _snap(env)
    store = ss.open_store(env)
    store.blob_path(_only_blob(store, "settings.toml", r.snapshot_id)).write_bytes(gzip.compress(b"rot"))
    (env / "settings.toml").write_text("current")
    res = ss.restore_snapshot(env, None, r.snapshot_id, apply=True, _services=lambda _s: [])
    assert not res.ok and "fails verification" in res.errors[0]
    assert (env / "settings.toml").read_text() == "current"  # untouched
    assert not list(env.rglob("*.pre-restore-*"))


def test_force_restores_the_intact_files_of_a_damaged_snapshot(env):
    _populate(env)
    r = _snap(env)
    store = ss.open_store(env)
    store.blob_path(_only_blob(store, "settings.toml", r.snapshot_id)).write_bytes(gzip.compress(b"rot"))
    (env / "settings.toml").write_text("current")
    (env / "sessions/s1.json").write_text("gone wrong")
    res = ss.restore_snapshot(env, None, r.snapshot_id, apply=True, force=True, _services=lambda _s: [])
    assert (env / "sessions/s1.json").read_text() == '{"turns": 4}'
    assert (env / "settings.toml").read_text() == "current"  # the rotted blob is refused per-file
    assert any("settings.toml" in e for e in res.errors) and not res.ok


def test_restore_refuses_while_a_service_is_running(env):
    _populate(env)
    r = _snap(env)
    (env / "settings.toml").write_text("current")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (env / "agents").mkdir(exist_ok=True)
        (env / "agents" / "svc.pid").write_text(str(child.pid))
        res = ss.restore_snapshot(env, None, r.snapshot_id, apply=True)
        assert not res.ok and "services appear to be running" in res.errors[0]
        assert (env / "settings.toml").read_text() == "current"
        forced = ss.restore_snapshot(env, None, r.snapshot_id, apply=True, force=True)
        assert forced.ok and (env / "settings.toml").read_text() == 'name = "x"\n'
    finally:
        child.kill()
        child.wait()


def test_a_dead_pid_file_does_not_block_restore(env):
    _populate(env)
    r = _snap(env)
    (env / "agents").mkdir(exist_ok=True)
    (env / "agents" / "old.pid").write_text("999999")
    (env / "settings.toml").write_text("current")
    assert ss.restore_snapshot(env, None, r.snapshot_id, apply=True).ok


def test_a_held_lock_file_counts_as_a_running_service(env):
    import fcntl

    _populate(env)
    lockp = env / "svc.lock"
    lockp.write_text("")
    child = subprocess.Popen([sys.executable, "-c",
                              "import fcntl,sys,time; f=open(sys.argv[1],'w'); "
                              "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(30)",
                              str(lockp)], stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == b"held"
        assert any("lock" in s.detail for s in ss.detect_running_services(env))
    finally:
        child.kill()
        child.wait()
    assert fcntl  # silence unused


def test_only_restricts_the_restore_to_matching_files(env):
    _populate(env)
    r = _snap(env)
    (env / "settings.toml").write_text("A")
    (env / "sessions/s1.json").write_text("B")
    res = ss.restore_snapshot(env, None, r.snapshot_id, only=["sessions/**"], apply=True, _services=lambda _s: [])
    assert res.ok
    assert (env / "sessions/s1.json").read_text() == '{"turns": 4}'
    assert (env / "settings.toml").read_text() == "A"


def test_only_matching_nothing_is_an_error_not_a_silent_success(env):
    _populate(env)
    r = _snap(env)
    res = ss.restore_snapshot(env, None, r.snapshot_id, only=["nope/**"], apply=True)
    assert not res.ok and "matched no file" in res.errors[0]


def _forge_path(store, sid, new_path):
    mp = store.snap_dir(sid) / "manifest.json"
    m = json.loads(mp.read_text())
    m["body"]["files"][0]["path"] = new_path
    m["body_sha256"], m["chain_hash"] = bc.manifest_chain(m["prev"], m["body"])
    mp.write_text(json.dumps(m))


@pytest.mark.parametrize("bad", ["../escape.txt", "/etc/escape.txt", "a/../../escape.txt", "a\\b", ""])
def test_a_tampered_manifest_cannot_write_outside_the_state_dir(env, bad):
    _populate(env)
    r = _snap(env)
    store = ss.open_store(env)
    _forge_path(store, r.snapshot_id, bad)
    with pytest.raises(ss.SnapshotError):
        ss.restore_snapshot(env, None, r.snapshot_id, apply=True, force=True, _services=lambda _s: [])
    assert not (env.parent / "escape.txt").exists()


def test_a_symlinked_directory_cannot_redirect_a_restore(env, tmp_path):
    _populate(env)
    r = _snap(env)
    outside = tmp_path / "outside"
    outside.mkdir()
    import shutil

    shutil.rmtree(env / "sessions")
    os.symlink(outside, env / "sessions")
    with pytest.raises(ss.SnapshotError):
        ss.restore_snapshot(env, None, r.snapshot_id, apply=True, _services=lambda _s: [])
    assert not list(outside.iterdir())


def test_post_restore_hash_check_puts_the_original_back(env, monkeypatch):
    """If the swapped-in file does not hash right, the original must come back."""
    _populate(env)
    r = _snap(env)
    (env / "settings.toml").write_text("precious current content")
    real = bc.sha256_file
    snapshot_sha = bc.sha256_bytes(b'name = "x"\n')

    def lying(p):  # the swapped-in file "does not hash right"
        h = real(p)
        return "0" * 64 if Path(p).name == "settings.toml" and h == snapshot_sha else h

    monkeypatch.setattr(bc, "sha256_file", lying)
    res = ss.restore_snapshot(env, None, r.snapshot_id, only=["settings.toml"], apply=True, force=True,
                              _services=lambda _s: [])
    assert not res.ok
    assert (env / "settings.toml").read_text() == "precious current content"


def test_restore_refuses_when_it_cannot_take_the_pre_restore_snapshot(env, monkeypatch):
    _populate(env)
    r = _snap(env)
    (env / "settings.toml").write_text("current")
    monkeypatch.setattr(bc, "free_bytes", lambda p: (1, 10**12))
    res = ss.restore_snapshot(env, None, r.snapshot_id, apply=True, _services=lambda _s: [])
    assert not res.ok and "pre-restore snapshot" in res.errors[0]
    assert (env / "settings.toml").read_text() == "current"


def test_resolve_latest_and_prefix_and_unknown(env):
    _populate(env)
    a = _snap(env).snapshot_id
    time.sleep(1.01)
    (env / "settings.toml").write_text("2")
    b = _snap(env).snapshot_id
    store = ss.open_store(env)
    assert ss.resolve_id(store, "latest") == b
    assert ss.resolve_id(store, a[:20]) == a
    with pytest.raises(ss.SnapshotError):
        ss.resolve_id(store, "nope")


def test_latest_verified_skips_a_corrupt_newest(env):
    _populate(env)
    a = _snap(env).snapshot_id
    time.sleep(1.01)
    (env / "settings.toml").write_text("2")
    b = _snap(env).snapshot_id
    store = ss.open_store(env)
    store.blob_path(_only_blob(store, "settings.toml", b)).write_bytes(gzip.compress(b"rot"))
    assert ss.resolve_id(store, "latest-verified") == a


# ===========================================================================
# prune
# ===========================================================================


def _aged_store(env, n=6):
    """n snapshots with distinct content, then rewrite manifests' created_at into the past."""
    _populate(env)
    pol = SnapshotPolicy()
    ids = []
    for i in range(n):
        (env / "settings.toml").write_text(f"v{i}")
        ids.append(_snap(env, policy=pol, prune=False).snapshot_id)
        time.sleep(1.01)
    return ids


def test_prune_dry_run_default_deletes_nothing_and_lists_reasons(env):
    ids = _aged_store(env)
    store = ss.open_store(env)
    pol = SnapshotPolicy(retention=RetentionPolicy(keep_last=1, keep_recent_hours=0, keep_daily=0,
                                                    keep_weekly=0, keep_monthly=0, protect_newest=1,
                                                    max_count=0, max_bytes=0))
    out = ss.prune_store(store, pol)  # apply defaults to False
    assert not out.applied and out.deleted == []
    assert len(out.plan.delete) == len(ids) - 1 and all(d.reasons for d in out.plan.delete)
    assert len(store.ids()) == len(ids)


def test_prune_apply_deletes_logs_every_decision_and_collects_blobs(env):
    ids = _aged_store(env)
    store = ss.open_store(env)
    pol = SnapshotPolicy(retention=RetentionPolicy(keep_last=2, keep_recent_hours=0, keep_daily=0,
                                                    keep_weekly=0, keep_monthly=0, protect_newest=2,
                                                    max_count=0, max_bytes=0))
    blobs_before = sum(1 for p in store.blobs.rglob("*") if p.is_file())
    out = ss.prune_store(store, pol, apply=True)
    assert len(out.deleted) == len(ids) - 2 and store.ids() == sorted(ids)[-2:]
    pruned = [e for e in store.ledger_entries() if e["event"] == "prune"]
    assert {e["id"] for e in pruned} == set(out.deleted) and all(e["detail"]["reasons"] for e in pruned)
    assert sum(1 for p in store.blobs.rglob("*") if p.is_file()) < blobs_before
    assert out.blobs_removed > 0 and ss.verify_store(store).ok  # survivors still intact after GC


def test_prune_keeps_pinned_even_when_policy_says_delete(env):
    ids = _aged_store(env)
    store = ss.open_store(env)
    ss.set_pinned(store, ids[0], True)
    pol = SnapshotPolicy(retention=RetentionPolicy(keep_last=1, keep_recent_hours=0, keep_daily=0,
                                                    keep_weekly=0, keep_monthly=0, protect_newest=1,
                                                    max_count=0, max_bytes=0))
    ss.prune_store(store, pol, apply=True)
    assert ids[0] in store.ids()
    ss.set_pinned(store, ids[0], False)
    ss.prune_store(store, pol, apply=True)
    assert ids[0] not in store.ids()


def test_prune_refuses_to_delete_when_nothing_verifies(env):
    ids = _aged_store(env, 4)
    store = ss.open_store(env)
    for sid in ids:  # make every snapshot unverifiable
        mp = store.snap_dir(sid) / "manifest.json"
        m = json.loads(mp.read_text())
        m["body"]["reason"] = "x"
        mp.write_text(json.dumps(m))
    pol = SnapshotPolicy(retention=RetentionPolicy(keep_last=0, keep_recent_hours=0, keep_daily=0,
                                                    keep_weekly=0, keep_monthly=0, protect_newest=1,
                                                    max_count=0, max_bytes=0))
    out = ss.prune_store(store, pol, apply=True)
    assert out.deleted == [] and sorted(store.ids()) == sorted(ids)


def test_auto_prune_runs_after_a_snapshot(env):
    _populate(env)
    pol = SnapshotPolicy(retention=RetentionPolicy(keep_last=1, keep_recent_hours=0, keep_daily=0,
                                                    keep_weekly=0, keep_monthly=0, protect_newest=1,
                                                    max_count=0, max_bytes=0))
    for i in range(3):
        (env / "settings.toml").write_text(f"v{i}")
        _snap(env, policy=pol)
        time.sleep(1.01)
    assert len(ss.open_store(env).ids()) == 1


# ===========================================================================
# db
# ===========================================================================


def test_with_db_refuses_the_default_developer_address(env):
    _populate(env)
    with pytest.raises(ss.SnapshotError, match="default developer database"):
        _snap(env, with_db=True, db_url="postgresql://u:p@localhost:5432/app")
    with pytest.raises(ss.SnapshotError, match="default developer database"):
        _snap(env, with_db=True, db_url="postgresql://u:p@127.0.0.1/app")
    assert ss.open_store(env).ids() == []  # and no half-snapshot was left


def test_with_db_requires_a_url_and_never_resolves_one_itself(env):
    _populate(env)
    with pytest.raises(ss.SnapshotError, match="no database URL"):
        _snap(env, with_db=True)


def test_with_db_records_the_dump_with_a_redacted_dsn(env):
    _populate(env)
    r = _snap(env, with_db=True, db_url="postgresql://u:hunter2@db.internal:5433/app",
              _db_dry_run=True)
    store = ss.open_store(env)
    db = store.read_manifest(r.snapshot_id)["body"]["db"]
    assert db["covered"] is True and db["dump"]["size"] > 0
    assert "hunter2" not in json.dumps(db) and "***" in db["dump"]["database"]
    assert ss.verify_snapshot(store, r.snapshot_id).ok
    (store.root / db["dump"]["relpath"]).write_bytes(b"tampered")
    assert not ss.verify_snapshot(store, r.snapshot_id).ok


def test_explicit_yes_this_database_allows_the_default_address(env):
    _populate(env)
    r = _snap(env, with_db=True, db_url="postgresql://u:p@localhost:5432/scratch",
              yes_this_database=True, _db_dry_run=True)
    assert r.db["covered"]


# ===========================================================================
# seams
# ===========================================================================


def test_latest_snapshot_for_returns_newest_intact_copy(env):
    _populate(env)
    a = _snap(env).snapshot_id
    time.sleep(1.01)
    (env / "settings.toml").write_text("2")
    b = _snap(env).snapshot_id
    ref = ss.latest_snapshot_for(env / "settings.toml", state_dir=env)
    assert ref and ref.snapshot_id == b and ref.path == "settings.toml"
    store = ss.open_store(env)
    store.blob_path(_only_blob(store, "settings.toml", b)).write_bytes(gzip.compress(b"rot"))
    again = ss.latest_snapshot_for("settings.toml", state_dir=env)
    assert again and again.snapshot_id == a  # falls back past the rotted copy


def test_latest_snapshot_for_unknown_or_foreign_paths(env, tmp_path):
    _populate(env)
    _snap(env)
    assert ss.latest_snapshot_for(env / "never-snapshotted.json", state_dir=env) is None
    with pytest.raises(ValueError):
        ss.latest_snapshot_for(tmp_path / "outside.json", state_dir=env)
    with pytest.raises(ss.SnapshotError):
        ss.latest_snapshot_for("../x", state_dir=env)
    assert ss.latest_snapshot_for("settings.toml", state_dir=env, root=tmp_path / "empty") is None


def test_restore_file_recovers_one_corrupt_file_and_keeps_the_corrupt_copy(env):
    _populate(env)
    r = _snap(env)
    (env / "orchestrator/approvals.json").write_text("{ torn write")
    ref = ss.restore_file(env / "orchestrator/approvals.json", state_dir=env)
    assert ref.snapshot_id == r.snapshot_id
    assert json.loads((env / "orchestrator/approvals.json").read_text()) == {"pending": [1, 2, 3]}
    kept = list((env / "orchestrator").glob("approvals.json.pre-restore-*"))
    assert len(kept) == 1 and kept[0].read_text() == "{ torn write"


def test_restore_file_recreates_a_missing_file_and_leaves_others_alone(env):
    _populate(env)
    _snap(env)
    (env / "sessions/s1.json").unlink()
    (env / "settings.toml").write_text("untouched newer")
    ss.restore_file("sessions/s1.json", state_dir=env)
    assert (env / "sessions/s1.json").read_text() == '{"turns": 4}'
    assert (env / "settings.toml").read_text() == "untouched newer"


def test_restore_file_without_an_intact_copy_raises(env):
    _populate(env)
    with pytest.raises(ss.SnapshotError, match="no snapshot holds an intact copy"):
        ss.restore_file("settings.toml", state_dir=env)


# ===========================================================================
# snapshot_before
# ===========================================================================


def test_snapshot_before_snapshots_then_runs_the_body(env, monkeypatch):
    monkeypatch.setenv("AXIOM_SNAPSHOT_BEFORE", "1")
    _populate(env)
    ran = []
    with ss.snapshot_before("ext install demo") as res:
        ran.append(res.snapshot_id if res else None)
    assert ran[0] and ss.open_store(env).read_manifest(ran[0])["body"]["trigger"] == "before"
    assert "ext install demo" in ss.open_store(env).read_manifest(ran[0])["body"]["reason"]


def test_snapshot_before_failure_is_loud_but_does_not_block_the_operation(env, monkeypatch, caplog):
    monkeypatch.setenv("AXIOM_SNAPSHOT_BEFORE", "1")
    _populate(env)
    monkeypatch.setattr(bc, "free_bytes", lambda p: (1, 10**12))
    said = []
    ran = []
    with ss.snapshot_before("upgrade", announce=said.append):
        ran.append(True)
    assert ran and any("COULD NOT take a state snapshot" in m for m in said)


def test_snapshot_before_strict_blocks_the_operation(env, monkeypatch):
    monkeypatch.setenv("AXIOM_SNAPSHOT_BEFORE", "1")
    _populate(env)
    monkeypatch.setattr(bc, "free_bytes", lambda p: (1, 10**12))
    ran = []
    with pytest.raises(ss.SnapshotError):
        with ss.snapshot_before("upgrade", strict=True):
            ran.append(True)  # pragma: no cover
    assert not ran


def test_the_test_session_leaves_snapshot_before_off_by_default():
    # the root conftest sets this so no suite snapshots a developer's real state dir
    assert os.environ.get("AXIOM_SNAPSHOT_BEFORE") == "0"


def test_snapshot_before_can_be_switched_off(env, monkeypatch):
    _populate(env)
    monkeypatch.setenv("AXIOM_SNAPSHOT_BEFORE", "0")
    with ss.snapshot_before("x") as res:
        assert res is None
    assert ss.open_store(env).ids() == []
    monkeypatch.setenv("AXIOM_SNAPSHOT_BEFORE", "1")
    (env / "settings.toml").write_text('[state.snapshots]\nenabled = false\n')
    with ss.snapshot_before("x") as res:
        assert res is None


# ===========================================================================
# emergency script: stdlib only, broken install
# ===========================================================================


def _run_script(*args, cwd):
    """Run the script under `python -I` with NO Axiom on the path and a poisoned environment."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME")}
    env["PYTHONPATH"] = ""
    return subprocess.run([sys.executable, "-I", str(SCRIPT), "state", *args], capture_output=True, text=True,
                          cwd=cwd, env=env, timeout=60)


def test_the_emergency_script_imports_nothing_from_axiom():
    src = SCRIPT.read_text()
    assert "import axiom" not in src and "from axiom" not in src
    import ast

    stdlib = set(sys.stdlib_module_names)
    tree = ast.parse(src)
    for node in tree.body:  # module level: standard library only
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] in stdlib for a in node.names), node.names
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in stdlib, node.module
    # `cryptography` is imported lazily, inside the one function the vault subcommands call
    lazy = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("cryptography")]
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "crypto")
    assert lazy and all(n in set(ast.walk(fn)) for n in lazy)


def test_end_to_end_snapshot_destroy_restore_with_the_stdlib_script(env, tmp_path):
    _populate(env)
    original = _tree(env)
    r = _snap(env)
    # destroy: delete half, corrupt the rest
    import shutil

    shutil.rmtree(env / "identity")
    shutil.rmtree(env / "sessions")
    (env / "settings.toml").write_text("GARBAGE")
    (env / "memory/audit.jsonl").write_text("")
    assert _tree(env) != original

    ls = _run_script("list", "--state", str(env), cwd=tmp_path)
    assert ls.returncode == 0 and r.snapshot_id in ls.stdout
    ver = _run_script("verify", "latest", "--state", str(env), cwd=tmp_path)
    assert ver.returncode == 0, ver.stdout + ver.stderr
    dry = _run_script("restore", "latest", "--state", str(env), cwd=tmp_path)
    assert dry.returncode == 0 and "DRY RUN" in dry.stdout and _tree(env) != original
    done = _run_script("restore", "latest", "--state", str(env), "--apply", cwd=tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _tree(env) == original
    assert stat.S_IMODE((env / "identity/identity.json").stat().st_mode) == 0o600
    assert (env / "settings.toml.pre-restore-" ).parent.is_dir()
    assert list(env.glob("settings.toml.pre-restore-*"))
    assert "next steps" in done.stdout


def test_the_script_refuses_a_bit_rotted_snapshot_and_finds_an_intact_one(env, tmp_path):
    _populate(env)
    a = _snap(env).snapshot_id
    time.sleep(1.01)
    (env / "settings.toml").write_text("2")
    b = _snap(env).snapshot_id
    store = ss.open_store(env)
    store.blob_path(_only_blob(store, "settings.toml", b)).write_bytes(gzip.compress(b"rot"))
    (env / "sessions/s1.json").write_text("x")
    bad = _run_script("restore", b, "--state", str(env), "--apply", cwd=tmp_path)
    assert bad.returncode == 1 and "REFUSING" in bad.stdout
    assert (env / "sessions/s1.json").read_text() == "x"
    good = _run_script("restore", "latest-verified", "--state", str(env), "--apply", cwd=tmp_path)
    assert good.returncode == 0 and (env / "sessions/s1.json").read_text() == '{"turns": 4}'
    assert a in good.stdout


def test_the_script_refuses_path_traversal_in_a_forged_manifest(env, tmp_path):
    _populate(env)
    r = _snap(env)
    _forge_path(ss.open_store(env), r.snapshot_id, "../escape.txt")
    out = _run_script("restore", "latest", "--state", str(env), "--apply", "--force", cwd=tmp_path)
    assert out.returncode != 0 and not (env.parent / "escape.txt").exists()


def test_the_script_works_with_axiom_deliberately_uninstallable(env, tmp_path):
    """A shadow `axiom` package that raises on import must not matter."""
    _populate(env)
    _snap(env)
    shadow = tmp_path / "shadow"
    (shadow / "axiom").mkdir(parents=True)
    (shadow / "axiom" / "__init__.py").write_text("raise RuntimeError('broken install')")
    (env / "settings.toml").write_text("bad")
    e = {k: v for k, v in os.environ.items() if k != "PYTHONHOME"}
    e["PYTHONPATH"] = str(shadow)
    out = subprocess.run([sys.executable, "-I", str(SCRIPT), "state", "restore", "latest", "--state", str(env), "--apply"],
                         capture_output=True, text=True, cwd=shadow, env=e, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (env / "settings.toml").read_text() == 'name = "x"\n'


def test_the_script_copy_travels_with_the_snapshots(env, tmp_path):
    _populate(env)
    dest = tmp_path / "other-disk"
    _snap(env, dest)
    copy = dest / "axi-restore.py"
    assert copy.is_file() and copy.read_bytes() == SCRIPT.read_bytes()
    assert os.access(copy, os.X_OK)


def test_script_and_module_agree_on_the_glob_semantics():
    import importlib.util

    spec = importlib.util.spec_from_file_location("emergency", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cases = [("a/**", "a/b/c"), ("**/*.log", "x/y/z.log"), ("**/*.log", "z.log"), ("*.toml", "a/b.toml"),
             ("identity/**", "identity"), ("a?c", "abc"), ("a?c", "a/c")]
    for pat, path in cases:
        assert mod.glob_match(pat, path) == ss.glob_match(pat, path), (pat, path)
