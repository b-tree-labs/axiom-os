# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every check here exists because the defect it names reached production.

A 5.3-million-chunk corpus grew to 73 GB with 60% of its rows unretrievable,
two byte-identical indexes, an ivfflat sized for a corpus it no longer had, and
a single file supplying 19.5% of the index. None of it was invisible — nothing
was looking.

So these are not hypotheses. Each test cites the incident, and each check is
paired with a negative control, because a check that cannot stay silent gets
ignored as fast as one that cannot fire.
"""

from __future__ import annotations

from axiom.extensions.builtins.hygiene.node_health import Severity
from axiom.rag.storage_health import (
    CHECK_PREFIX,
    CorpusShape,
    DeploymentFacts,
    IndexFacts,
    StorageFacts,
    check_corpus_bound,
    check_database_memory_limit,
    check_database_rollout_strategy,
    check_duplicate_index_expressions,
    check_guard_enabled,
    check_ivfflat_lists,
    check_never_scanned_indexes,
    check_shared_memory,
    check_single_file_share,
    run_all,
)

# --- duplicate index expressions ----------------------------------------------
#
# Live defect: idx_chunks_fts and idx_chunks_tsv held the identical GIN
# expression, 1.79 GB each. CREATE INDEX IF NOT EXISTS guards the NAME, so a
# renamed index silently becomes a second copy and the DDL rebuilds it forever.

GIN = "USING gin (to_tsvector('english'::regconfig, chunk_text))"


def test_duplicate_expression_is_found_under_different_names():
    findings = check_duplicate_index_expressions(
        [
            IndexFacts(
                "idx_chunks_fts", f"CREATE INDEX idx_chunks_fts ON chunks {GIN}", 1_876_951_040, 112
            ),
            IndexFacts(
                "idx_chunks_tsv",
                f"CREATE INDEX idx_chunks_tsv ON chunks {GIN}",
                1_875_902_464,
                1904,
            ),
        ]
    )
    assert len(findings) == 1
    assert "idx_chunks_fts" in findings[0].message and "idx_chunks_tsv" in findings[0].message


def test_duplicate_finding_names_the_one_to_drop_and_it_is_the_less_used():
    """Dropping the index the planner has been choosing is a latency incident
    dressed as a cleanup."""
    findings = check_duplicate_index_expressions(
        [
            IndexFacts("keep_me", f"CREATE INDEX keep_me ON chunks {GIN}", 100, 5000),
            IndexFacts("drop_me", f"CREATE INDEX drop_me ON chunks {GIN}", 100, 3),
        ]
    )
    ev = findings[0].expected_value
    # Both are named: the operator needs to know what survives as much as what
    # goes. What matters is which side of "drop"/"keep" each one lands on.
    assert ev.index("drop_me") < ev.index("keep_me")
    assert ev.startswith("drop drop_me")
    assert "keep keep_me" in ev


def test_distinct_expressions_are_not_duplicates():
    """The negative control. Two GIN indexes on different columns are two
    indexes, and a check that flags them is a check people switch off."""
    assert (
        check_duplicate_index_expressions(
            [
                IndexFacts(
                    "a",
                    "CREATE INDEX a ON chunks USING gin (to_tsvector('english', chunk_text))",
                    1,
                    1,
                ),
                IndexFacts(
                    "b",
                    "CREATE INDEX b ON chunks USING gin (to_tsvector('english', source_title))",
                    1,
                    1,
                ),
            ]
        )
        == []
    )


# --- never-scanned indexes ----------------------------------------------------


def test_never_scanned_index_is_reported_but_never_as_auto_fixable():
    """ "Never scanned" is not evidence of "never needed" — an index may serve a
    feature that has not shipped — so this reports and a human decides.

    A busy sibling is present because the check needs a denominator; see the
    reset-invariance tests below for why.
    """
    findings = check_never_scanned_indexes(
        [
            IndexFacts("idx_chunks_source_path", "CREATE INDEX ...", 19_000_000, 22_227),
            IndexFacts("idx_chunks_cognitive_type", "CREATE INDEX ...", 77_000_000, 0),
        ]
    )
    assert len(findings) == 1
    assert findings[0].auto_fixable is False


def test_a_scanned_index_is_silent():
    assert (
        check_never_scanned_indexes(
            [
                IndexFacts("busy", "CREATE INDEX ...", 1, 22_227),
                IndexFacts("used", "CREATE INDEX ...", 1, 1),
            ]
        )
        == []
    )


# --- ivfflat lists vs corpus size ---------------------------------------------
#
# Live defect: the index was built with lists=1000 for 5.3M vectors. After the
# reap the corpus was 2.1M and nothing re-evaluated the parameter. The schema
# DDL hardcodes lists=100 while recommended_lists() sits unused beside it.


def test_ivfflat_lists_far_from_recommended_is_reported():
    f = check_ivfflat_lists(rows=2_098_293, lists=100)
    assert f is not None
    assert "100" in f.current_value
    assert "1449" in f.expected_value  # round(sqrt(2098293))


def test_ivfflat_lists_close_enough_is_silent():
    """Within a factor of two is not worth a rebuild, and a check that fires on
    every small drift trains people to ignore it."""
    assert check_ivfflat_lists(rows=2_098_293, lists=1449) is None
    assert check_ivfflat_lists(rows=2_098_293, lists=1000) is None


def test_ivfflat_check_is_silent_on_an_empty_corpus():
    """Schema creation builds the index on an empty table. Reporting drift
    against zero rows would make every fresh install start with a finding."""
    assert check_ivfflat_lists(rows=0, lists=100) is None


# --- amplification: one file dominating the index -----------------------------
#
# Live defect: one 10 MB MATLAB workspace became 1,036,802 chunks, 19.5% of the
# corpus, while the median file contributed 15. Nothing noticed for months.


def test_a_single_file_dominating_the_corpus_is_reported():
    f = check_single_file_share(
        CorpusShape(
            total_chunks=5_304_997,
            distinct_files=22_355,
            median_chunks_per_file=15,
            largest_file="…/NC3.txt",
            largest_file_chunks=1_036_802,
        )
    )
    assert f is not None
    assert "NC3.txt" in f.message
    assert "19" in f.message  # the share


def test_an_evenly_spread_corpus_is_silent():
    assert (
        check_single_file_share(
            CorpusShape(
                total_chunks=100_000,
                distinct_files=2_000,
                median_chunks_per_file=40,
                largest_file="a.pdf",
                largest_file_chunks=900,
            )
        )
        is None
    )


def test_a_tiny_corpus_does_not_trip_on_its_first_document():
    """One document in a new corpus is 100% of it. Firing there would make the
    check meaningless on exactly the installs that need onboarding to be quiet."""
    assert (
        check_single_file_share(
            CorpusShape(
                total_chunks=40,
                distinct_files=1,
                median_chunks_per_file=40,
                largest_file="only.pdf",
                largest_file_chunks=40,
            )
        )
        is None
    )


# --- the declared bound (M6) --------------------------------------------------


def test_a_corpus_over_its_declared_bound_is_a_finding_with_an_owner():
    f = check_corpus_bound(total_bytes=80_000_000_000, max_bytes=60_000_000_000, owner="@ben.booth")
    assert f is not None
    assert "@ben.booth" in f.message


def test_a_corpus_under_its_bound_is_silent():
    """The negative control that makes the bound a bound rather than a banner."""
    assert check_corpus_bound(total_bytes=10, max_bytes=60_000_000_000, owner="@ben.booth") is None


def test_a_bound_with_no_owner_is_itself_the_finding():
    """An unowned cost is discovered as an outage. That has now happened once."""
    f = check_corpus_bound(total_bytes=10, max_bytes=60_000_000_000, owner="")
    assert f is not None
    assert "owner" in f.message.lower()


def test_no_declared_bound_is_reported_rather_than_passed_over():
    f = check_corpus_bound(total_bytes=80_000_000_000, max_bytes=None, owner="")
    assert f is not None
    assert "no bound" in f.message.lower() or "undeclared" in f.message.lower()


# --- the guard itself ---------------------------------------------------------


def test_a_disabled_ingestion_guard_is_reported():
    """The escape hatch exists so an operator can unblock an ingest. Left on,
    it silently restores the behaviour that produced the 73 GB corpus."""
    f = check_guard_enabled(enabled=False)
    assert f is not None
    assert "AXIOM_RAG_INGEST_GUARD" in f.message


def test_an_enabled_guard_is_silent():
    assert check_guard_enabled(enabled=True) is None


# --- /dev/shm -----------------------------------------------------------------
#
# Live defect: VACUUM (ANALYZE) failed with "could not resize shared memory
# segment ... No space left on device". The postgres pod had Kubernetes' default
# 64 MB /dev/shm. Maintenance could not run on the table that most needed it.


def test_default_64mb_dev_shm_is_reported():
    f = check_shared_memory(shm_bytes=64 * 1024 * 1024)
    assert f is not None
    assert "64" in f.current_value
    assert f.auto_fixable is False  # needs a pod spec change and a restart


def test_adequate_dev_shm_is_silent():
    assert check_shared_memory(shm_bytes=1024 * 1024 * 1024) is None


def test_unknown_dev_shm_is_not_reported_as_broken():
    """Not every deployment is a container, and 'could not measure' must never
    read as 'misconfigured'."""
    assert check_shared_memory(shm_bytes=None) is None


# --- run_all: what the heartbeat actually calls --------------------------------


def _healthy_facts(**over):
    base = dict(
        indexes=[
            IndexFacts("idx_chunks_fts", "CREATE INDEX idx_chunks_fts ON chunks " + GIN, 1, 9)
        ],
        shape=CorpusShape(
            total_chunks=2_098_293,
            distinct_files=22_226,
            median_chunks_per_file=15,
            largest_file="a.pdf",
            largest_file_chunks=900,
        ),
        rows=2_098_293,
        ivfflat_lists=1449,
        total_bytes=10,
        max_bytes=60_000_000_000,
        owner="@ben.booth",
        guard_enabled=True,
        shm_bytes=1024 * 1024 * 1024,
    )
    base.update(over)
    return StorageFacts(**base)


def test_a_healthy_store_produces_no_findings():
    """The control for the whole module. If this cannot be silent, none of the
    individual negative controls matter — the report is noise either way."""
    assert run_all(_healthy_facts()) == []


def test_run_all_surfaces_every_independent_problem_not_just_the_first():
    """A store with several defects must report several. Returning the first
    means each fix reveals the next one a week later."""
    findings = run_all(
        _healthy_facts(
            ivfflat_lists=100,
            guard_enabled=False,
            shm_bytes=64 * 1024 * 1024,
            max_bytes=None,
            owner="",
        )
    )
    checks = {f.check for f in findings}
    assert f"{CHECK_PREFIX}_ivfflat_lists" in checks
    assert f"{CHECK_PREFIX}_ingest_guard" in checks
    assert f"{CHECK_PREFIX}_shared_memory" in checks
    assert f"{CHECK_PREFIX}_corpus_bound" in checks


def test_every_finding_carries_the_module_prefix():
    """So an operator can filter the retrieval store's findings out of a node
    report covering a dozen subsystems."""
    for f in run_all(_healthy_facts(ivfflat_lists=100, guard_enabled=False)):
        assert f.check.startswith(CHECK_PREFIX)


def test_run_all_on_an_empty_corpus_is_silent():
    """A fresh install must not open with a page of findings."""
    empty = _healthy_facts(
        rows=0,
        total_bytes=0,
        indexes=[],
        ivfflat_lists=100,
        max_bytes=None,
        owner="",
        shape=CorpusShape(0, 0, 0, "", 0),
    )
    assert run_all(empty) == []


# --- how the database is DEPLOYED ---------------------------------------------
#
# Live incident, 2026-09-23. Patching the postgres Deployment started a second
# postmaster on the same ReadWriteOnce PVC, because the deployment used
# RollingUpdate with maxSurge 25%. The old pod's shutdown removed
# postmaster.pid and the new one killed itself mid-VACUUM:
#
#   could not open file "postmaster.pid": No such file or directory
#   performing immediate shutdown because data directory lock file is invalid
#
# Postgres's lock file is the only thing that prevented two postmasters writing
# one data directory. The repository's own helm charts had this right; the node
# was running a hand-applied manifest that predated them. Nothing compared the
# two, so the drift was invisible until it bit.


def test_a_database_deployment_that_can_run_two_pods_is_critical():
    f = DeploymentFacts(
        kind="Deployment", strategy="RollingUpdate", mounts_pvc=True, memory_limit_bytes=2 * 1024**3
    )
    finding = check_database_rollout_strategy(f)
    assert finding is not None
    assert finding.severity is Severity.CRITICAL
    assert "Recreate" in finding.expected_value


def test_recreate_on_a_pvc_backed_deployment_is_silent():
    assert (
        check_database_rollout_strategy(
            DeploymentFacts(
                kind="Deployment",
                strategy="Recreate",
                mounts_pvc=True,
                memory_limit_bytes=2 * 1024**3,
            )
        )
        is None
    )


def test_a_statefulset_is_silent_whatever_its_strategy():
    """A StatefulSet gives each replica its own volume, so rolling it does not
    put two writers on one data directory. Flagging it would be noise."""
    assert (
        check_database_rollout_strategy(
            DeploymentFacts(
                kind="StatefulSet",
                strategy="RollingUpdate",
                mounts_pvc=True,
                memory_limit_bytes=2 * 1024**3,
            )
        )
        is None
    )


def test_a_deployment_with_no_persistent_volume_is_silent():
    """Two stateless replicas are the point of a Deployment."""
    assert (
        check_database_rollout_strategy(
            DeploymentFacts(
                kind="Deployment",
                strategy="RollingUpdate",
                mounts_pvc=False,
                memory_limit_bytes=2 * 1024**3,
            )
        )
        is None
    )


def test_unknown_deployment_shape_is_not_reported_as_broken():
    """Not every install is Kubernetes. "Could not measure" is never
    "misconfigured"."""
    assert check_database_rollout_strategy(None) is None


# --- memory limit against the data it serves ----------------------------------
#
# The same pod was capped at 2Gi while serving a 435 GB database on a host with
# 498 GB of RAM. A maintenance_work_mem large enough to rebuild an index
# OOM-killed it, which is how the first reindex attempt died.


def test_a_database_capped_far_below_its_data_is_reported():
    f = DeploymentFacts(
        kind="Deployment", strategy="Recreate", mounts_pvc=True, memory_limit_bytes=2 * 1024**3
    )
    finding = check_database_memory_limit(f, database_bytes=435 * 1000**3)
    assert finding is not None
    assert "2.0 GB" in finding.current_value


def test_a_reasonably_sized_database_container_is_silent():
    f = DeploymentFacts(
        kind="Deployment", strategy="Recreate", mounts_pvc=True, memory_limit_bytes=16 * 1024**3
    )
    assert check_database_memory_limit(f, database_bytes=435 * 1000**3) is None


def test_no_memory_limit_at_all_is_silent():
    """An uncapped container is a scheduling decision, not a defect this check
    can judge."""
    f = DeploymentFacts(
        kind="Deployment", strategy="Recreate", mounts_pvc=True, memory_limit_bytes=None
    )
    assert check_database_memory_limit(f, database_bytes=435 * 1000**3) is None


def test_a_small_database_in_a_small_container_is_silent():
    """The ratio is the signal, not the absolute size. A 2 GB container serving
    200 MB is fine, and firing there would make every dev install noisy."""
    f = DeploymentFacts(
        kind="Deployment", strategy="Recreate", mounts_pvc=True, memory_limit_bytes=2 * 1024**3
    )
    assert check_database_memory_limit(f, database_bytes=200 * 1000**2) is None


# --- never-scanned, after maintenance resets the counters ---------------------
#
# Found by running the check right after a VACUUM FULL. The rewrite gives every
# index a new relfilenode and pg_stat drops the old row, so idx_scan resets:
# idx_chunks_source_path went 362,385 -> 22,227 and three indexes that had been
# used read 0. pg_stat_database.stats_reset still said "never", so that is NOT
# the signal.
#
# The reset-invariant question is not "how long have we been counting" but
# "how many chances did this index have". All counters reset together, so peer
# scans on the same table are the denominator.


def test_zero_scans_is_not_reported_when_the_table_is_barely_queried():
    """Right after a rebuild every counter is near zero. Reporting then names
    indexes that are fine and trains the reader to skip the section."""
    fresh = [
        IndexFacts("busy", "CREATE INDEX ...", 1_000_000, 12),
        IndexFacts("cold", "CREATE INDEX ...", 1_000_000, 0),
    ]
    assert check_never_scanned_indexes(fresh) == []


def test_zero_scans_is_reported_once_the_table_has_had_real_traffic():
    """22,227 chances taken by a sibling and none by this one is evidence,
    whenever the counting started."""
    observed = [
        IndexFacts("idx_chunks_source_path", "CREATE INDEX ...", 19_000_000, 22_227),
        IndexFacts("idx_chunks_cognitive_type", "CREATE INDEX ...", 14_000_000, 0),
    ]
    findings = check_never_scanned_indexes(observed)
    assert len(findings) == 1
    assert "idx_chunks_cognitive_type" in findings[0].message


def test_the_finding_says_how_many_chances_were_missed():
    """Without the denominator the reader cannot judge the claim, and an
    unjudgeable finding is one they take on trust once and ignore after."""
    findings = check_never_scanned_indexes(
        [
            IndexFacts("busy", "CREATE INDEX ...", 1, 22_227),
            IndexFacts("cold", "CREATE INDEX ...", 1, 0),
        ]
    )
    assert "22,227" in findings[0].message
