# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Keep watching the retrieval store after it is installed.

Every check here was written against a defect that had already reached
production and sat there. A live corpus grew to 5,304,997 chunks and 73 GB
with:

- two byte-identical GIN indexes, 1.79 GB each;
- an ivfflat index sized for a corpus it no longer had;
- one 10 MB binary artifact supplying 19.5% of the index while the median file
  supplied 15 chunks;
- 60% of its rows unretrievable, proven by deleting them and measuring no
  change in hit@5;
- a postgres pod with Kubernetes' default 64 MB ``/dev/shm``, so ``VACUUM``
  could not run on the table that needed it most.

None of that was hidden. Nothing was looking. Installation-time correctness is
not the same property as correctness three months later, and only the second
one matters to the person who has to answer a question.

**Every check has a negative control in the tests.** A check that cannot stay
silent is ignored exactly as fast as a check that cannot fire, and the
difference is invisible in a green report.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from axiom.extensions.builtins.hygiene.node_health import Finding, Severity

CHECK_PREFIX = "rag_storage"

#: An ivfflat index is worth rebuilding when its ``lists`` is off the
#: recommended value by more than this factor. Recall degrades gradually, so a
#: tighter bound would demand rebuilds that buy nothing and teach operators to
#: skip the report.
IVFFLAT_DRIFT_FACTOR = 2.0

#: One source file supplying more than this share of a corpus is amplification,
#: not a big document.
SINGLE_FILE_SHARE_LIMIT = 0.10

#: Below this many files, share is meaningless — the first document in a new
#: corpus is all of it.
MIN_FILES_FOR_SHARE = 20

#: Peer scans on the same table required before "never scanned" means anything.
#: All of a table's index counters reset together — a VACUUM FULL or REINDEX
#: gives each index a new relfilenode and pg_stat drops the old row — and
#: `pg_stat_database.stats_reset` does NOT record it. So the reset-invariant
#: question is not "how long have we been counting" but "how many chances did
#: this index have", and peer scans are the denominator.
MIN_PEER_SCANS_FOR_COLD = 1000

#: Below this, maintenance (parallel VACUUM, index builds) fails on the table
#: that most needs it. Kubernetes defaults ``/dev/shm`` to 64 MB.
MIN_SHM_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class IndexFacts:
    """One index as ``pg_stat_user_indexes`` and ``pg_get_indexdef`` report it."""

    name: str
    definition: str
    size_bytes: int
    scans: int

    @property
    def expression(self) -> str:
        """The definition with its own name removed, so two indexes that differ
        only by name compare equal."""
        return re.sub(r"^CREATE\s+(UNIQUE\s+)?INDEX\s+\S+\s+", "", self.definition).strip()


@dataclass(frozen=True)
class CorpusShape:
    """What the corpus looks like, not just how big it is.

    Size alone did not reveal the defect that mattered: 73 GB looked like a
    large corpus rather than one file repeated into the index.
    """

    total_chunks: int
    distinct_files: int
    median_chunks_per_file: int
    largest_file: str
    largest_file_chunks: int


def _f(
    check: str,
    severity: Severity,
    message: str,
    current: str = "",
    expected: str = "",
    auto_fixable: bool = False,
) -> Finding:
    return Finding(
        check=f"{CHECK_PREFIX}_{check}",
        severity=severity,
        message=message,
        current_value=current,
        expected_value=expected,
        auto_fixable=auto_fixable,
    )


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def check_duplicate_index_expressions(indexes: list[IndexFacts]) -> list[Finding]:
    """Two indexes with the same expression under different names.

    ``CREATE INDEX IF NOT EXISTS`` guards the **name**, not the expression, so
    an index renamed by hand or by an older schema version silently becomes a
    second copy that the DDL then rebuilds on every schema-ensure forever. That
    is how 1.79 GB was paid for twice.

    The finding names the **less-scanned** member as the one to drop, so the
    planner keeps the index it has been choosing. Dropping the used one is a
    latency incident dressed as a cleanup.
    """
    groups: dict[str, list[IndexFacts]] = {}
    for idx in indexes:
        groups.setdefault(idx.expression, []).append(idx)

    findings = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda i: i.scans, reverse=True)
        keep, doomed = ordered[0], ordered[1:]
        wasted = sum(d.size_bytes for d in doomed)
        findings.append(
            _f(
                "duplicate_index",
                Severity.WARNING,
                f"{len(members)} indexes share one expression: "
                f"{', '.join(i.name + f' ({i.scans} scans)' for i in ordered)}. "
                f"{_human(wasted)} is paid for twice. Note that CREATE INDEX IF NOT "
                f"EXISTS guards the NAME, so dropping the duplicate is not enough if "
                f"the surviving index is not the one the schema DDL creates — rename "
                f"it to that name, or the next schema-ensure rebuilds the duplicate.",
                current=f"{len(members)} copies, {_human(wasted)} redundant",
                expected=f"drop {', '.join(d.name for d in doomed)}; keep {keep.name}",
            )
        )
    return findings


def check_never_scanned_indexes(indexes: list[IndexFacts]) -> list[Finding]:
    """Indexes the planner has never chosen, when that means something.

    Reported, never auto-fixed. An index may serve a feature that has not
    shipped; "never scanned" is evidence that a human should look, not a
    verdict.

    **Silent until the table has had real traffic.** Every index counter on a
    table resets together when the table is rewritten, and nothing records that
    it happened, so immediately after a VACUUM FULL every index reads zero.
    Peer scans are the reset-invariant denominator: 22,227 chances taken by a
    sibling and none by this one is evidence whenever the counting began.
    """
    if not indexes:
        return []
    peak = max(i.scans for i in indexes)
    if peak < MIN_PEER_SCANS_FOR_COLD:
        return []
    idle = [i for i in indexes if i.scans == 0]
    if not idle:
        return []
    return [
        _f(
            "never_scanned_index",
            Severity.INFO,
            f"{len(idle)} index(es) have never been scanned while a sibling on the "
            f"same table took {peak:,} scans: "
            f"{', '.join(f'{i.name} ({_human(i.size_bytes)})' for i in idle)}. "
            f"An unshipped feature is a legitimate reason to keep one — a human decides.",
            current=f"{_human(sum(i.size_bytes for i in idle))} in unscanned indexes",
            expected="each reviewed: kept with a reason, or dropped",
        )
    ]


def check_ivfflat_lists(rows: int, lists: int) -> Finding | None:
    """Is the vector index still sized for the corpus it has?

    ``lists`` is chosen when the index is built, which is usually when the
    table is empty. The corpus then grows by two orders of magnitude, or is
    reaped back down, and nothing re-evaluates it.
    """
    if rows <= 0:
        return None  # a fresh install builds on an empty table; not drift
    recommended = max(100, round(math.sqrt(rows)))
    if (
        recommended / max(lists, 1) <= IVFFLAT_DRIFT_FACTOR
        and lists / max(recommended, 1) <= IVFFLAT_DRIFT_FACTOR
    ):
        return None
    return _f(
        "ivfflat_lists",
        Severity.WARNING,
        f"ivfflat lists={lists} was chosen for a different corpus: {rows:,} rows "
        f"now recommend ~{recommended}. Recall is roughly probes/lists, so a "
        f"stale value costs accuracy or speed depending on its direction.",
        current=f"lists={lists} for {rows:,} rows",
        expected=f"lists={recommended} (REINDEX CONCURRENTLY, or rebuild at the next window)",
    )


def check_single_file_share(shape: CorpusShape) -> Finding | None:
    """One source file dominating the index.

    This is the check that would have caught the incident. A 10 MB MATLAB
    workspace was text-extracted and chunked into 1,036,802 windows — 19.5% of
    the corpus, against a median of 15 chunks per file. Total size looked like
    a large corpus. The *shape* was one file repeated into the index.
    """
    if shape.distinct_files < MIN_FILES_FOR_SHARE or shape.total_chunks <= 0:
        return None
    share = shape.largest_file_chunks / shape.total_chunks
    if share <= SINGLE_FILE_SHARE_LIMIT:
        return None
    return _f(
        "single_file_share",
        Severity.WARNING,
        f"one source file is {share:.1%} of the index: {shape.largest_file} "
        f"contributed {shape.largest_file_chunks:,} chunks against a median of "
        f"{shape.median_chunks_per_file}. That ratio is text extraction of a "
        f"binary or tabular artifact, not a long document.",
        current=f"{share:.1%} from one file",
        expected=f"no single file above {SINGLE_FILE_SHARE_LIMIT:.0%}; check what "
        f"the extractor accepted and whether it belongs in the signals tables",
    )


def check_corpus_bound(total_bytes: int, max_bytes: int | None, owner: str) -> Finding | None:
    """A corpus declares a size budget, and somebody owns it.

    An unowned cost is discovered as an outage. A bound with no owner produces
    a finding nobody clears, which is the same thing one report later.
    """
    if max_bytes is None:
        return _f(
            "corpus_bound",
            Severity.WARNING,
            f"this corpus has no declared bound and currently holds "
            f"{_human(total_bytes)}. Undeclared growth is discovered as a full disk.",
            current=_human(total_bytes),
            expected="declare max_bytes and an owner for this corpus",
        )
    if not owner:
        return _f(
            "corpus_bound",
            Severity.WARNING,
            f"this corpus declares a bound of {_human(max_bytes)} but names no "
            f"owner. A finding with no owner is not actionable and will be "
            f"filtered out of the report within a month.",
            current="bound declared, owner empty",
            expected="name the principal accountable for this corpus",
        )
    if total_bytes <= max_bytes:
        return None
    return _f(
        "corpus_bound",
        Severity.CRITICAL,
        f"corpus is {_human(total_bytes)} against a declared bound of "
        f"{_human(max_bytes)} — {owner} owns this.",
        current=_human(total_bytes),
        expected=f"at or below {_human(max_bytes)}",
    )


def check_guard_enabled(enabled: bool) -> Finding | None:
    """Is the ingestion guard still on?

    It ships with an off switch on purpose, so an operator can push a blocked
    corpus through. Left off, it silently restores the behaviour that produced
    a corpus 60% of which was unretrievable.
    """
    if enabled:
        return None
    return _f(
        "ingest_guard",
        Severity.WARNING,
        "the ingestion guard is disabled (AXIOM_RAG_INGEST_GUARD=0). Content "
        "that retrieval cannot use will be embedded and indexed again. This is "
        "a deliberate escape hatch, so the question is whether it was left on.",
        current="disabled",
        expected="enabled, unless an operator is deliberately overriding it now",
    )


def check_shared_memory(shm_bytes: int | None) -> Finding | None:
    """Can maintenance actually run against this database?

    Kubernetes gives a pod 64 MB of ``/dev/shm`` by default. Parallel ``VACUUM``
    on the largest table then fails with "could not resize shared memory
    segment", so the table that most needs maintenance is the one that cannot
    get it — and the failure appears only when somebody tries.

    ``None`` means "could not measure", which is not "misconfigured". Not every
    deployment is a container.
    """
    if shm_bytes is None:
        return None
    if shm_bytes >= MIN_SHM_BYTES:
        return None
    return _f(
        "shared_memory",
        Severity.WARNING,
        f"the database has {_human(shm_bytes)} of /dev/shm, which is the "
        f"container default. Parallel VACUUM and index builds fail against a "
        f"large table with 'could not resize shared memory segment'. Raising it "
        f"needs a pod spec change and a restart, so it is worth doing before the "
        f"table is large rather than during the incident.",
        current=_human(shm_bytes),
        expected=f"at least {_human(MIN_SHM_BYTES)} (emptyDir with medium: Memory, sizeLimit set)",
    )


@dataclass(frozen=True)
class DeploymentFacts:
    """How the database is deployed, not how it is configured.

    Two of the worst defects found on a live node were neither in the schema
    nor in postgresql.conf: a Deployment that could run two postmasters against
    one volume, and a container capped at 2 GiB while serving 435 GB on a host
    with 498 GB of RAM.
    """

    kind: str
    strategy: str
    mounts_pvc: bool
    memory_limit_bytes: int | None


#: A database container this far below the size of the data it serves cannot
#: hold a working set, and cannot be given a maintenance_work_mem large enough
#: to rebuild an index without being OOM-killed. Deliberately a ratio: a 2 GiB
#: container serving 200 MB is fine, and an absolute floor would make every
#: development install noisy.
MEMORY_TO_DATA_RATIO_FLOOR = 1 / 100


def check_database_rollout_strategy(facts: DeploymentFacts | None) -> Finding | None:
    """Can this deployment ever run two writers against one data directory?

    A `Deployment` with `RollingUpdate` starts the replacement pod **before**
    the old one terminates. On a ReadWriteOnce volume both mount the same
    directory, and two postmasters on one PGDATA is how a database is
    corrupted. Observed on a live node:

        could not open file "postmaster.pid": No such file or directory
        performing immediate shutdown because data directory lock file is invalid

    The lock file is the only thing that stopped it. That is a safety net, not
    a design.

    A StatefulSet is silent: each replica gets its own volume. A Deployment
    with no PVC is silent: two stateless replicas are the point.
    """
    if facts is None:
        return None
    if facts.kind != "Deployment" or not facts.mounts_pvc:
        return None
    if facts.strategy == "Recreate":
        return None
    return _f(
        "db_rollout_strategy",
        Severity.CRITICAL,
        f"the database is a Deployment with strategy={facts.strategy or 'RollingUpdate'} "
        f"mounting a persistent volume, so a rollout starts a second pod against "
        f"the same data directory before the first exits. Postgres's lock file is "
        f"the only thing preventing two postmasters writing one PGDATA, and it "
        f"aborts whichever process is running at the time.",
        current=f"{facts.kind}, strategy={facts.strategy or 'RollingUpdate'}, PVC-backed",
        expected="strategy: Recreate (or a StatefulSet, which gives each replica its own volume)",
    )


def check_database_memory_limit(
    facts: DeploymentFacts | None, database_bytes: int
) -> Finding | None:
    """Is the database container sized for the data it serves?

    An uncapped container is a scheduling decision this check cannot judge, so
    ``None`` is silent. The signal is the ratio, not the absolute size.
    """
    if facts is None or not facts.memory_limit_bytes or database_bytes <= 0:
        return None
    if facts.memory_limit_bytes / database_bytes >= MEMORY_TO_DATA_RATIO_FLOOR:
        return None
    return _f(
        "db_memory_limit",
        Severity.WARNING,
        f"the database container is limited to {_human(facts.memory_limit_bytes)} "
        f"while serving {_human(database_bytes)}. It cannot hold a useful working "
        f"set, and a maintenance_work_mem large enough to rebuild an index will "
        f"OOM-kill it — which takes the database down rather than failing the "
        f"maintenance.",
        current=_human(facts.memory_limit_bytes),
        expected=f"at least {_human(database_bytes * MEMORY_TO_DATA_RATIO_FLOOR)} "
        f"({MEMORY_TO_DATA_RATIO_FLOOR:.0%} of the data), and check what the host actually has",
    )


@dataclass(frozen=True)
class StorageFacts:
    """Everything the checks need, gathered once.

    A dataclass rather than a live cursor so the heartbeat's checks are pure
    and the tests are hermetic. Collecting is the part that touches the world;
    judging is not.
    """

    indexes: list[IndexFacts]
    shape: CorpusShape
    rows: int
    ivfflat_lists: int
    total_bytes: int
    max_bytes: int | None
    owner: str
    guard_enabled: bool
    shm_bytes: int | None


def run_all(facts: StorageFacts) -> list[Finding]:
    """Every configuration check, against one snapshot.

    Returns **all** independent problems, not the first. A store with several
    defects that reports one means each fix reveals the next a week later, and
    the operator learns the report is never finished.

    An empty corpus returns nothing at all: a fresh install must not open with
    a page of findings, or the report is discredited before it is ever right.
    """
    if facts.rows <= 0:
        return []

    findings: list[Finding] = []
    findings.extend(check_duplicate_index_expressions(facts.indexes))
    findings.extend(check_never_scanned_indexes(facts.indexes))
    for single in (
        check_ivfflat_lists(facts.rows, facts.ivfflat_lists),
        check_single_file_share(facts.shape),
        check_corpus_bound(facts.total_bytes, facts.max_bytes, facts.owner),
        check_guard_enabled(facts.guard_enabled),
        check_shared_memory(facts.shm_bytes),
    ):
        if single is not None:
            findings.append(single)
    return findings


__all__ = [
    "CHECK_PREFIX",
    "IVFFLAT_DRIFT_FACTOR",
    "MIN_FILES_FOR_SHARE",
    "MIN_PEER_SCANS_FOR_COLD",
    "MIN_SHM_BYTES",
    "SINGLE_FILE_SHARE_LIMIT",
    "CorpusShape",
    "DeploymentFacts",
    "MEMORY_TO_DATA_RATIO_FLOOR",
    "IndexFacts",
    "StorageFacts",
    "check_corpus_bound",
    "check_database_memory_limit",
    "check_database_rollout_strategy",
    "check_duplicate_index_expressions",
    "check_guard_enabled",
    "check_ivfflat_lists",
    "check_never_scanned_indexes",
    "check_shared_memory",
    "check_single_file_share",
    "run_all",
]
