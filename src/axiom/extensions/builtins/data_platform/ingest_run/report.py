# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``IngestRunReport`` — the generic, job-agnostic ingest stage funnel.

A job opens a run, records counts per stage as work flows through, then
finishes it. The report is the single source of truth for "what happened
in this run": how many items entered each stage, how many advanced, how
many were dropped (and why), how many failed (and why).

Nothing here is RAG- or source-specific. A job declares its own ordered
``stages`` (or uses :data:`DEFAULT_STAGES`); a job that only fetches+lands
omits ``extracted``/``indexed`` and the funnel still reads cleanly.

Design notes:
- **Counts, not rows.** The funnel is aggregate. Per-item detail belongs
  in the job's own records / logs; this primitive answers "is the pipeline
  healthy and where do items disappear?".
- **Reasons are bucketed.** ``dropped`` and ``failed`` are dicts keyed by a
  short reason/cause string (``"unchanged"``, ``"unsupported"``, ``"401"``,
  ``"embed_failed"``) → count, so the funnel shows *why* without unbounded
  cardinality.
- **Monotonic, append-only during a run.** Helpers only increment; the
  report is finalized once via :meth:`finish`.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Conventional stage order for a full extract→index pipeline. A job uses a
# prefix/subset of these (or its own list). Kept as plain strings so a job
# can introduce a stage this list doesn't know about without a code change.
DEFAULT_STAGES: tuple[str, ...] = (
    "discovered",    # items the source reports exist
    "to_process",    # after change-detection (new/changed; unchanged dropped)
    "fetched",       # bytes pulled to the landing zone
    "extracted",     # text/fields extracted from bytes (by type)
    "transformed",   # chunked / normalized / shaped
    "loaded",        # written to the durable store (bronze/table)
    "indexed",       # made searchable (embedded + upserted), if applicable
)


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"      # the run itself errored / was refused
    PARTIAL = "partial"    # finished, but some items dropped/failed


@dataclass
class StageCounts:
    """In/out + dropped/failed for one stage of one run.

    ``entered`` items either ``advanced`` to the next stage, were
    ``dropped`` (expected: skipped-with-reason), or ``failed``
    (unexpected: failed-with-cause). ``advanced + sum(dropped) +
    sum(failed)`` should reconcile to ``entered`` for a well-behaved job,
    but the primitive does not enforce it (a job may not account for every
    item) — :meth:`unaccounted` exposes any gap for diagnostics.
    """

    stage: str
    entered: int = 0
    advanced: int = 0
    # reason/cause → count, so the funnel shows WHY without per-item rows.
    dropped: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)

    @property
    def dropped_total(self) -> int:
        return sum(self.dropped.values())

    @property
    def failed_total(self) -> int:
        return sum(self.failed.values())

    def unaccounted(self) -> int:
        """entered − (advanced + dropped + failed); 0 when fully accounted."""
        return self.entered - (self.advanced + self.dropped_total + self.failed_total)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "entered": self.entered,
            "advanced": self.advanced,
            "dropped": dict(self.dropped),
            "failed": dict(self.failed),
            "dropped_total": self.dropped_total,
            "failed_total": self.failed_total,
            "unaccounted": self.unaccounted(),
        }


@dataclass
class IngestRunReport:
    """A single ingest run's stage funnel — job-agnostic.

    Open with :meth:`start`, record counts as work flows, close with
    :meth:`finish`. ``job_kind`` is a free string identifying the kind of
    job (e.g. ``"pull"``, ``"push"``, ``"cdc"``, or a consumer's own); the
    funnel never branches on it — it's a label for the operator.
    """

    run_id: str
    job_kind: str
    source: str = ""
    stages: tuple[str, ...] = DEFAULT_STAGES
    status: RunStatus = RunStatus.RUNNING
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    refused_reason: str = ""
    _counts: dict[str, StageCounts] = field(default_factory=dict)
    # Free-form, job-supplied extras (e.g. bytes fetched, extractor breakdown).
    metrics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def start(
        cls,
        job_kind: str,
        *,
        source: str = "",
        stages: tuple[str, ...] | None = None,
        run_id: str | None = None,
    ) -> IngestRunReport:
        rep = cls(
            run_id=run_id or uuid.uuid4().hex,
            job_kind=job_kind,
            source=source,
            stages=tuple(stages) if stages else DEFAULT_STAGES,
        )
        for s in rep.stages:
            rep._counts[s] = StageCounts(stage=s)
        return rep

    # ---- recording (increment-only during a run) ------------------------

    def _stage(self, stage: str) -> StageCounts:
        sc = self._counts.get(stage)
        if sc is None:  # a job may record a stage not in its declared list
            sc = StageCounts(stage=stage)
            self._counts[stage] = sc
        return sc

    def entered(self, stage: str, n: int = 1) -> None:
        """Record ``n`` items arriving at ``stage``."""
        self._stage(stage).entered += n

    def advanced(self, stage: str, n: int = 1) -> None:
        """Record ``n`` items leaving ``stage`` toward the next."""
        self._stage(stage).advanced += n

    def dropped(self, stage: str, reason: str, n: int = 1) -> None:
        """Record ``n`` items intentionally skipped at ``stage`` (with reason)."""
        d = self._stage(stage).dropped
        d[reason] = d.get(reason, 0) + n

    def failed(self, stage: str, cause: str, n: int = 1) -> None:
        """Record ``n`` items that errored at ``stage`` (with cause)."""
        f = self._stage(stage).failed
        f[cause] = f.get(cause, 0) + n

    def set_metric(self, key: str, value: Any) -> None:
        self.metrics[key] = value

    def add_metric(self, key: str, n: int = 1) -> None:
        self.metrics[key] = self.metrics.get(key, 0) + n

    # ---- finalize -------------------------------------------------------

    def refuse(self, reason: str) -> IngestRunReport:
        """Mark the whole run refused (e.g. a volume gate said no)."""
        self.refused_reason = reason
        self.status = RunStatus.FAILED
        self.finished_at = time.time()
        return self

    def finish(self, *, failed: bool = False) -> IngestRunReport:
        """Close the run. Status is FAILED if ``failed``, else PARTIAL when any
        stage dropped/failed items, else SUCCEEDED."""
        self.finished_at = time.time()
        if failed:
            self.status = RunStatus.FAILED
        elif self.refused_reason:
            self.status = RunStatus.FAILED
        elif any(sc.dropped_total or sc.failed_total for sc in self._counts.values()):
            self.status = RunStatus.PARTIAL
        else:
            self.status = RunStatus.SUCCEEDED
        return self

    # ---- views ----------------------------------------------------------

    @property
    def duration_s(self) -> float | None:
        if self.finished_at is None:
            return None
        return round(self.finished_at - self.started_at, 3)

    @property
    def total_failed(self) -> int:
        return sum(sc.failed_total for sc in self._counts.values())

    @property
    def total_dropped(self) -> int:
        return sum(sc.dropped_total for sc in self._counts.values())

    def stage(self, name: str) -> StageCounts | None:
        return self._counts.get(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "job_kind": self.job_kind,
            "source": self.source,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": self.duration_s,
            "refused_reason": self.refused_reason,
            "total_dropped": self.total_dropped,
            "total_failed": self.total_failed,
            # Emit the funnel in declared order, then any extra stages a job
            # recorded that weren't in its declared list.
            "funnel": [
                self._counts[s].to_dict()
                for s in self.stages
                if s in self._counts
            ] + [
                sc.to_dict()
                for name, sc in self._counts.items()
                if name not in self.stages
            ],
            "metrics": dict(self.metrics),
        }

    def render(self) -> str:
        """A compact human funnel for CLI / logs."""
        lines = [
            f"ingest run {self.run_id[:8]} [{self.job_kind}]"
            + (f" source={self.source}" if self.source else "")
            + f" — {self.status.value}"
            + (f" ({self.duration_s}s)" if self.duration_s is not None else "")
        ]
        if self.refused_reason:
            lines.append(f"  REFUSED: {self.refused_reason}")
        for s in self.stages:
            sc = self._counts.get(s)
            if sc is None or (sc.entered == 0 and sc.advanced == 0
                              and not sc.dropped and not sc.failed):
                continue
            seg = f"  {s:<12} in={sc.entered:<6} out={sc.advanced:<6}"
            if sc.dropped:
                seg += f" dropped={sc.dropped_total}{_reasons(sc.dropped)}"
            if sc.failed:
                seg += f" failed={sc.failed_total}{_reasons(sc.failed)}"
            lines.append(seg)
        return "\n".join(lines)


def _reasons(d: dict[str, int]) -> str:
    inner = ", ".join(f"{k}:{v}" for k, v in sorted(d.items()))
    return f"({inner})"


__all__ = ["DEFAULT_STAGES", "IngestRunReport", "RunStatus", "StageCounts"]
