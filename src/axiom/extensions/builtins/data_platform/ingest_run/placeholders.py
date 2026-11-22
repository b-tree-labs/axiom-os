# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Placeholders found by ingest: counted per run, kept per source, alerted once.

The bronze gate recognises a placeholder (a Git LFS pointer, a ``.pdf`` that is
not a PDF) and records it in the bronze manifest (#1948). This module is the
ingest run's side of that:

* :class:`PlaceholderTally` counts what one run found, item by item.
* :class:`PlaceholderLedger` is the per-source ingest health record: the
  placeholders each source still holds, written by ingest as it lands items.
  An item drops out when its real file lands. A health check reads this record;
  it never re-derives the count by scanning bronze manifests or silver rows
  (ADR-128: a check reads a projection, it does not compute over a working
  tier).
* :func:`close_run` writes the ledger, stamps the run's funnel and raises ONE
  alert per source per run that found any, naming the count, one example path
  and the recovery (one incident per cause, not one per file).

Domain-agnostic: it speaks source / item / path / reason.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The HERALD intent of the per-run alert.
ALERT_INTENT = "data.ingest.placeholders"

#: The recovery, said the same way in the alert, the diagnose finding and the
#: troubleshoot step.
RECOVERY = (
    "The source holds placeholders, not the files: re-upload the real files at "
    "the source. For Git LFS pointers, run `git lfs pull` in the clone the files "
    "were copied from (or `git lfs push --all` from a clone that has the objects) "
    "and copy them up again. The next pull lands them and indexes them; nothing "
    "needs deleting here."
)

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class PlaceholderTally:
    """What one ingest run found for one source. Thread-safe."""

    source: str
    found: dict[str, tuple[str, str]] = field(default_factory=dict)  # item_id -> (path, reason)
    cleared: set[str] = field(default_factory=set)  # item_ids whose real file landed
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def saw(self, item_id: str, path: str, reason: str) -> None:
        with self._lock:
            self.found[item_id] = (path, reason)
            self.cleared.discard(item_id)

    def landed(self, item_id: str) -> None:
        """A real (non-placeholder) file landed for ``item_id``."""
        with self._lock:
            self.cleared.add(item_id)
            self.found.pop(item_id, None)

    @property
    def count(self) -> int:
        return len(self.found)

    def kinds(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _path, reason in self.found.values():
            out[reason] = out.get(reason, 0) + 1
        return out

    def example(self) -> str | None:
        """The first placeholder this run found."""
        return next((path for path, _ in self.found.values()), None)


class PlaceholderLedger:
    """Per-source record of placeholders ingest has landed and not yet replaced.

    One JSON file per source under ``root``. Written by ingest only.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @classmethod
    def for_state_dir(cls, state_dir: str | Path | None = None) -> PlaceholderLedger:
        if state_dir is None:
            from axiom.infra.paths import get_user_state_dir

            state_dir = get_user_state_dir()
        return cls(Path(state_dir) / "ingest_health")

    def _path(self, source: str) -> Path:
        return self.root / f"{_SAFE.sub('_', source or 'unnamed')[:128]}.placeholders.json"

    def _load(self, source: str) -> dict[str, Any]:
        path = self._path(source)
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def apply(self, tally: PlaceholderTally) -> None:
        """Fold one run's findings into the source's record."""
        if not tally.found and not tally.cleared:
            return
        doc = self._load(tally.source)
        items: dict[str, dict[str, Any]] = dict(doc.get("items") or {})
        now = time.time()
        for item_id in tally.cleared:
            items.pop(item_id, None)
        for item_id, (path, reason) in tally.found.items():
            items[item_id] = {"path": path, "reason": reason, "seen_at": now}
        path = self._path(tally.source)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(
            json.dumps({"source": tally.source, "updated_at": now, "items": items}),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def summary(self) -> list[dict[str, Any]]:
        """One row per source still holding placeholders: count, example, kinds."""
        rows: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return rows
        for path in sorted(self.root.glob("*.placeholders.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items = doc.get("items") or {}
            if not items:
                continue
            kinds: dict[str, int] = {}
            for entry in items.values():
                reason = str(entry.get("reason") or "placeholder")
                kinds[reason] = kinds.get(reason, 0) + 1
            rows.append(
                {
                    "source": doc.get("source") or path.name.split(".placeholders")[0],
                    "count": len(items),
                    # The earliest still held (the record keeps arrival order).
                    "example": str(next(iter(items.values())).get("path") or ""),
                    "kinds": kinds,
                    "updated_at": doc.get("updated_at"),
                }
            )
        return rows


def _alert(tally: PlaceholderTally, run_id: str | None) -> str | None:
    from axiom.extensions.builtins.notifications.incidents.model import Cause, Unmet

    from .. import _herald

    n = tally.count
    example = tally.example()
    kinds = ", ".join(
        f"{k.removeprefix('placeholder: ')} x{v}" for k, v in sorted(tally.kinds().items())
    )
    summary = (
        f"{tally.source}: {n} placeholder file(s) landed instead of documents "
        f"(e.g. {example}); not indexed"
    )
    cause = Cause(
        area="data",
        kind="placeholder_content",
        action="data.ingest",
        subject=tally.source,
        error_class="PlaceholderContent",
        unmet=(
            Unmet(id="real_files", why=f"{n} item(s) are placeholders ({kinds})", fix=RECOVERY),
        ),
        evidence_ref=f"ingest-run:{run_id}" if run_id else "",
        detail={"count": n, "example": example, "kinds": tally.kinds()},
    )
    return _herald.publish_event(
        ALERT_INTENT,
        summary,
        body=f"Kinds: {kinds}.\n\n{RECOVERY}",
        priority="high",
        dedup_key=f"{ALERT_INTENT}:{tally.source}:{run_id or int(time.time())}",
        payload={"source": tally.source, "count": n, "example": example},
        cause=cause,
    )


def close_run(
    tally: PlaceholderTally,
    *,
    ledger: PlaceholderLedger | None,
    run: Any = None,
) -> None:
    """Finish one run's placeholder accounting. Never raises: telemetry and
    alerting must not sink an ingest run."""
    if run is not None:
        try:
            run.set_metric("placeholders", tally.count)
        except Exception:  # noqa: BLE001
            pass
    if ledger is not None:
        try:
            ledger.apply(tally)
        except Exception as exc:  # noqa: BLE001
            log.warning("placeholder ledger write failed for %s: %s", tally.source, exc)
    if tally.count:
        log.warning(
            "ingest %s: %d placeholder file(s), e.g. %s", tally.source, tally.count, tally.example()
        )
        try:
            _alert(tally, getattr(run, "run_id", None))
        except Exception as exc:  # noqa: BLE001
            log.warning("placeholder alert failed for %s: %s", tally.source, exc)


def placeholder_finding(state_dir: str | Path | None = None) -> dict[str, Any]:
    """The ``data diagnose`` finding: placeholder counts per source, read from
    the ledger ingest writes. Never reads bronze or silver."""
    rows = PlaceholderLedger.for_state_dir(state_dir).summary()
    per_source = {r["source"]: r["count"] for r in rows}
    if not rows:
        return {
            "check": "placeholders",
            "ok": True,
            "per_source": {},
            "detail": "no source holds placeholder files",
        }
    lines = [f"{r['source']}: {r['count']} placeholder file(s), e.g. {r['example']}" for r in rows]
    return {
        "check": "placeholders",
        "ok": False,
        "per_source": per_source,
        "detail": "; ".join(lines),
        "remediation": RECOVERY,
    }


__all__ = [
    "ALERT_INTENT",
    "RECOVERY",
    "PlaceholderLedger",
    "PlaceholderTally",
    "close_run",
    "placeholder_finding",
]
