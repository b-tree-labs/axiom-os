# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every check here was written against a measurement taken on a live node.

On 2026-10-05, ahead of onboarding three partner data-acquisition nodes, the
deployed data platform was measured for the first time:

- ``axiom_db`` was **528 GB** on a volume **91% full**, growing 3.4 GB/day —
  about 90 days of runway, and nothing was reporting that;
- its dominant object was 246 GB of table and 203 GB of index over 860,487,014
  rows, a **plain unpartitioned table**, although TimescaleDB 2.28.2 was
  installed and idle. Measured compression on one real hour: **113.9x**;
- ``shared_buffers`` was **128 MB** on a machine with **498 GB of RAM** — the
  stock default, 0.026% of memory, against a 528 GB database;
- ``n_dead_tup`` was 129,957,106 (13.1%) with ``last_vacuum`` and
  ``last_autovacuum`` both NULL. Autovacuum was not broken: the trigger is
  ``50 + 0.2 x 860,487,014`` = 172 M dead tuples, so it had simply never fired,
  and when it did it would vacuum 246 GB while serving partners.

None of that was hidden and nothing was looking. The hygiene surface already
reported disk *percentage*, which cannot tell you when you run out, and the one
module that did this kind of work — ``axiom.rag.storage_health`` — only ever
looked at the retrieval store.

**Every check has a negative control.** A check that cannot stay silent is
ignored exactly as fast as one that cannot fire, and the difference is invisible
in a green report.
"""

from __future__ import annotations

from axiom.extensions.builtins.hygiene.node_health import Severity
from axiom.infra import store_health as sh

GB = 1024**3

def _rel(**over):
    base = dict(
        schema="public", name="reactor_timeseries_default", rows=860_487_014,
        table_bytes=246 * GB, index_bytes=203 * GB, dead_tuples=0,
        ever_vacuumed=True, is_hypertable=True, compression_enabled=True,
        time_column_indexed=True,
    )
    base.update(over)
    return sh.RelationFacts(**base)

def _server(**over):
    base = dict(
        shared_buffers_bytes=4 * GB, effective_memory_bytes=16 * GB,
        work_mem_bytes=256 * 1024**2, maintenance_work_mem_bytes=8 * GB,
        autovacuum_enabled=True, autovacuum_scale_factor=0.01,
        autovacuum_threshold=50, timescale_installed=True,
    )
    base.update(over)
    return sh.ServerFacts(**base)

def _volume(**over):
    base = dict(mount="/home", total_bytes=3300 * GB, free_bytes=2000 * GB,
                daily_growth_bytes=3.4 * GB)
    base.update(over)
    return sh.VolumeFacts(**base)

# ---------------------------------------------------------------------------
# shared_buffers
# ---------------------------------------------------------------------------

def test_stock_shared_buffers_on_a_large_host_is_flagged():
    """128 MB of 498 GB. The exact value found on the node."""
    f = sh.check_shared_buffers(_server(shared_buffers_bytes=128 * 1024**2))
    assert f is not None
    assert f.severity is Severity.WARNING
    assert "128" in f.current_value or "0.0" in f.current_value

def test_a_well_sized_shared_buffers_is_silent():
    assert sh.check_shared_buffers(_server()) is None

def test_a_small_host_is_not_nagged():
    """A laptop or a small container legitimately runs a small buffer pool."""
    assert sh.check_shared_buffers(
        _server(shared_buffers_bytes=128 * 1024**2, effective_memory_bytes=4 * GB)) is None

# ---------------------------------------------------------------------------
# dead tuples and vacuum
# ---------------------------------------------------------------------------

def test_never_vacuumed_with_heavy_bloat_is_critical():
    """Both last_vacuum and last_autovacuum NULL at 13% dead, as found."""
    f = sh.check_dead_tuples(_rel(dead_tuples=129_957_106, ever_vacuumed=False))
    assert f is not None
    assert f.severity is Severity.CRITICAL
    assert "never" in f.message.lower()

def test_bloat_on_a_vacuumed_table_is_only_a_warning():
    f = sh.check_dead_tuples(_rel(dead_tuples=129_957_106, ever_vacuumed=True))
    assert f is not None and f.severity is Severity.WARNING

def test_a_clean_table_is_silent():
    assert sh.check_dead_tuples(_rel(dead_tuples=1000)) is None

def test_an_empty_table_does_not_divide_by_zero():
    assert sh.check_dead_tuples(_rel(rows=0, dead_tuples=0)) is None

# ---------------------------------------------------------------------------
# autovacuum reachability — the subtle one
# ---------------------------------------------------------------------------

def test_a_trigger_no_table_will_reach_in_time_is_flagged():
    """0.2 x 860 M = 172 M dead tuples before autovacuum fires. Not broken.
    Just never going to happen before the vacuum itself becomes an incident."""
    f = sh.check_autovacuum_reachable(_rel(), _server(autovacuum_scale_factor=0.2))
    assert f is not None
    assert "172" in f.current_value or "172" in f.message

def test_a_tuned_scale_factor_is_silent():
    assert sh.check_autovacuum_reachable(_rel(), _server(autovacuum_scale_factor=0.01)) is None

def test_autovacuum_switched_off_entirely_is_critical():
    f = sh.check_autovacuum_reachable(_rel(), _server(autovacuum_enabled=False))
    assert f is not None and f.severity is Severity.CRITICAL

def test_a_small_table_is_fine_on_stock_settings():
    """Stock 0.2 is correct for most tables; only size makes it wrong."""
    assert sh.check_autovacuum_reachable(
        _rel(rows=100_000), _server(autovacuum_scale_factor=0.2)) is None

# ---------------------------------------------------------------------------
# uncompressed timeseries
# ---------------------------------------------------------------------------

def test_a_large_plain_table_with_timescale_installed_is_flagged():
    """The finding worth 113.9x."""
    f = sh.check_timeseries_storage(_rel(is_hypertable=False), _server())
    assert f is not None
    assert "hypertable" in f.message.lower()

def test_a_hypertable_without_compression_is_flagged():
    f = sh.check_timeseries_storage(
        _rel(is_hypertable=True, compression_enabled=False), _server())
    assert f is not None
    assert "compress" in f.message.lower()

def test_a_compressed_hypertable_is_silent():
    assert sh.check_timeseries_storage(_rel(), _server()) is None

def test_without_timescale_installed_we_do_not_demand_it():
    """Telling an operator to use an extension they have not installed is noise."""
    assert sh.check_timeseries_storage(
        _rel(is_hypertable=False), _server(timescale_installed=False)) is None

def test_a_big_table_with_an_unindexed_timestamp_is_not_told_to_become_a_hypertable():
    """Found by running against the live store twice. First a 13 GB RAG chunk
    table and a file manifest were advised to convert on size alone. Then, with
    a has-a-timestamp-column gate, all ten tables examined still passed it,
    because almost every table carries a created_at. Only an *indexed* timestamp
    separated `ts` from `indexed_at` and `fetched_at`."""
    assert sh.check_timeseries_storage(
        _rel(name="chunks", is_hypertable=False, time_column_indexed=False,
             table_bytes=13 * GB, index_bytes=0), _server()) is None


def test_an_indexed_time_column_on_a_large_table_does_flag():
    """The true positive this gate must not suppress."""
    f = sh.check_timeseries_storage(
        _rel(is_hypertable=False, time_column_indexed=True), _server())
    assert f is not None


def test_the_autovacuum_suggestion_lands_somewhere_useful():
    """Deriving it from the ceiling produced 0.1889 against a current 0.2 —
    correct and useless."""
    rel = _rel(rows=105_853_105)
    f = sh.check_autovacuum_reachable(rel, _server(autovacuum_scale_factor=0.2))
    assert f is not None
    suggested = float(f.expected_value.split("=")[1].strip().split()[0])
    assert suggested < 0.1, f.expected_value
    assert suggested * rel.rows <= sh.AUTOVACUUM_TARGET_DEAD_ROWS * 1.05


def test_a_small_plain_table_is_left_alone():
    assert sh.check_timeseries_storage(
        _rel(is_hypertable=False, table_bytes=100 * 1024**2, index_bytes=0), _server()) is None

# ---------------------------------------------------------------------------
# days to full — a percentage cannot tell you when you run out
# ---------------------------------------------------------------------------

def test_ninety_days_of_runway_is_flagged():
    """307 GB free at 3.4 GB/day, as found on the node."""
    f = sh.check_days_to_full(_volume(free_bytes=307 * GB))
    assert f is not None
    assert "90" in f.current_value or "90" in f.message

def test_two_weeks_of_runway_is_critical():
    f = sh.check_days_to_full(_volume(free_bytes=48 * GB))
    assert f is not None and f.severity is Severity.CRITICAL

def test_years_of_runway_is_silent():
    assert sh.check_days_to_full(_volume(free_bytes=2000 * GB)) is None

def test_an_unknown_growth_rate_reports_nothing_rather_than_guessing():
    """A growth rate we have not measured must not become a forecast."""
    assert sh.check_days_to_full(_volume(daily_growth_bytes=None)) is None

def test_a_shrinking_volume_does_not_forecast_the_past():
    assert sh.check_days_to_full(_volume(daily_growth_bytes=0)) is None

# ---------------------------------------------------------------------------
# run_all
# ---------------------------------------------------------------------------

def test_run_all_on_the_node_as_it_was_found_reports_every_problem():
    """The real 2026-10-05 snapshot. If this returns fewer than four findings,
    a check regressed."""
    facts = sh.StoreFacts(
        server=_server(shared_buffers_bytes=128 * 1024**2,
                       work_mem_bytes=16 * 1024**2,
                       maintenance_work_mem_bytes=1 * GB,
                       autovacuum_scale_factor=0.2),
        relations=[_rel(dead_tuples=129_957_106, ever_vacuumed=False,
                        is_hypertable=False, compression_enabled=False)],
        volumes=[_volume(free_bytes=307 * GB)],
    )
    found = sh.run_all(facts)
    checks = {f.check for f in found}
    assert len(found) >= 4, found
    assert any("shared_buffers" in c for c in checks)
    assert any("vacuum" in c for c in checks)
    assert any("timeseries" in c or "compress" in c for c in checks)
    assert any("full" in c or "headroom" in c for c in checks)
    assert all(c.startswith(sh.CHECK_PREFIX) for c in checks), checks

def test_run_all_on_a_healthy_store_is_silent():
    """The negative control for the whole module."""
    facts = sh.StoreFacts(server=_server(), relations=[_rel()], volumes=[_volume()])
    assert sh.run_all(facts) == []

def test_run_all_tolerates_no_relations():
    assert sh.run_all(sh.StoreFacts(server=_server(), relations=[], volumes=[])) == []

# ---------------------------------------------------------------------------
# Wiring. A check nothing calls is not shipped.
# ---------------------------------------------------------------------------

def test_audit_node_actually_runs_these_checks():
    """The module could be perfect and still useless. This is the only test
    that proves an operator running the audit sees these findings."""
    from axiom.extensions.builtins.hygiene import node_health as nh

    facts = sh.StoreFacts(
        server=_server(shared_buffers_bytes=128 * 1024**2),
        relations=[], volumes=[],
    )
    report = nh.audit_node(
        run=lambda *a, **k: (1, ""),
        boot_list_output="",
        store_facts=facts,
    )
    checks = {f.check for f in report.findings}
    assert f"{sh.CHECK_PREFIX}_shared_buffers" in checks, sorted(checks)

def test_check_data_store_never_raises_on_a_broken_collector(monkeypatch):
    """An unreachable store must not take down the rest of the audit."""
    from axiom.extensions.builtins.hygiene import node_health as nh

    def boom():
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(nh, "_collect_store_facts", boom)
    assert nh.check_data_store() == []

# ---------------------------------------------------------------------------
# Growth sampling — a rate needs history, and one point is not a rate
# ---------------------------------------------------------------------------

def test_one_sample_yields_no_rate(tmp_path, monkeypatch):
    from axiom.extensions.builtins.hygiene import node_health as nh

    monkeypatch.setenv("AXIOM_STATE_DIR", str(tmp_path))
    assert nh._record_store_sample(100 * GB) is None

def test_two_samples_a_day_apart_yield_the_measured_rate(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from axiom.extensions.builtins.hygiene import node_health as nh

    monkeypatch.setenv("AXIOM_STATE_DIR", str(tmp_path))
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    assert nh._record_store_sample(100 * GB, now=t0) is None
    rate = nh._record_store_sample(110 * GB, now=t0 + timedelta(days=2))
    assert rate is not None
    assert abs(rate - 5 * GB) < 0.01 * GB, rate

def test_a_baseline_of_minutes_is_refused(tmp_path, monkeypatch):
    """Extrapolating a day's growth from ten minutes of ingest forecasts
    nonsense in whichever direction the last write went."""
    from datetime import UTC, datetime, timedelta

    from axiom.extensions.builtins.hygiene import node_health as nh

    monkeypatch.setenv("AXIOM_STATE_DIR", str(tmp_path))
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    nh._record_store_sample(100 * GB, now=t0)
    assert nh._record_store_sample(101 * GB, now=t0 + timedelta(minutes=10)) is None

def test_a_shrinking_database_reports_no_rate(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from axiom.extensions.builtins.hygiene import node_health as nh

    monkeypatch.setenv("AXIOM_STATE_DIR", str(tmp_path))
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    nh._record_store_sample(200 * GB, now=t0)
    assert nh._record_store_sample(150 * GB, now=t0 + timedelta(days=2)) is None

def test_a_corrupt_sample_file_does_not_stop_the_audit(tmp_path, monkeypatch):
    from axiom.extensions.builtins.hygiene import node_health as nh

    monkeypatch.setenv("AXIOM_STATE_DIR", str(tmp_path))
    path = nh._store_sample_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json at all")
    assert nh._record_store_sample(100 * GB) is None
    assert path.exists(), "the corrupt file was not replaced with a usable one"


# ---------------------------------------------------------------------------
# A declared capacity nothing enforces — the earliest catch available
# ---------------------------------------------------------------------------


def test_a_pvc_declaring_ten_gigs_while_holding_five_hundred_is_critical():
    """The real shape: a PersistentVolume declared 10Gi, local-path ignored it,
    and `kubectl get pvc` reported 10Gi Bound while 528 GB sat on the host."""
    f = sh.check_declared_capacity(
        _volume(declared_capacity_bytes=10 * GB), used_bytes=528 * GB)
    assert f is not None
    assert f.severity is Severity.CRITICAL
    assert "declaration" in f.message.lower() or "enforce" in f.message.lower()


def test_modest_overshoot_is_tolerated():
    assert sh.check_declared_capacity(
        _volume(declared_capacity_bytes=100 * GB), used_bytes=110 * GB) is None


def test_no_declaration_means_nothing_to_compare():
    assert sh.check_declared_capacity(_volume(), used_bytes=528 * GB) is None


def test_sizing_a_buffer_pool_uses_the_pod_limit_not_host_ram():
    """498 GB of host RAM behind a 16 GiB pod limit. Recommending 25% of the
    host would recommend a value that OOM-kills the database."""
    f = sh.check_shared_buffers(
        _server(shared_buffers_bytes=128 * 1024**2, effective_memory_bytes=16 * GB))
    assert f is not None
    assert "4GB" in f.expected_value, f.expected_value
    assert "498" not in f.expected_value


def test_an_unknown_memory_limit_is_reported_not_assumed():
    """Silence here would read as a pass, and a buffer pool sized for another
    machine would stay wrong forever."""
    f = sh.check_shared_buffers(_server(effective_memory_bytes=None))
    assert f is not None
    assert f.severity is Severity.INFO
    assert "not checked" in f.message.lower() or "could not" in f.message.lower()


def test_an_explicit_memory_limit_override_is_honoured(monkeypatch):
    from axiom.extensions.builtins.hygiene import node_health as nh

    monkeypatch.setenv("AXIOM_DB_MEMORY_LIMIT_BYTES", str(16 * GB))
    assert nh._database_memory_limit_bytes() == 16 * GB


def test_a_nonsense_override_is_refused_rather_than_trusted(monkeypatch):
    from axiom.extensions.builtins.hygiene import node_health as nh

    monkeypatch.setenv("AXIOM_DB_MEMORY_LIMIT_BYTES", "lots")
    assert nh._database_memory_limit_bytes() is None
