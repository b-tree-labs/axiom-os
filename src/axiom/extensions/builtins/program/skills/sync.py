# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.sync`` — reconcile the data file against its source(s), log the diff.

The self-update step (prd-program R3/R4, R11). One pass per source:

1. Read the current state from a *source* (:mod:`.sources`). The default
   source is the node's own ``data.json`` (self-reconcile); an explicit
   ``source`` file, or a live ``gitlab`` / ``github`` feeder selected by
   ``source_kind``, stands in as an upstream.
2. A live source first climbs the connector-readiness ladder (R9); one that
   cannot be verified is **skipped loudly** — recorded in ``skipped`` with the
   rung it failed — never treated as "no changes".
3. Update ``data.json`` when a distinct source's content differs.
4. Diff the source against the last snapshot and append one change-log entry
   per difference, then advance the snapshot.

Reconciliation is **idempotent**: the snapshot is a content hash per item and
per field, so running ``sync`` twice over an unchanged source logs nothing
the second time. It assumes it is not the only writer — each reconcile runs
under an exclusive lock on the snapshot, so concurrent syncs serialize rather
than double-log.

``source_kind`` selects the feeder (``file`` | ``gitlab`` | ``github`` |
``all``). With no selection it reads ``program.feeders`` from the data file,
falling back to self-reconcile so CLERK's heartbeat stays a safe local no-op
until a deployment opts a live feeder in. This is the action the CLERK agent
runs on its heartbeat; posting back to a tracker is a later phase.

When the data file names a ``program.writer`` and this install is not it,
``sync`` runs read-only (ADR-248): it reads and diffs, reports
``would_change`` and ``read_only``, and writes nothing.

**On the schedule (prd-program R28).** The scheduler runs this skill on the
program's own cadence (``program.cadence``, see ``..cadence``) with
``single_flight``: a run takes a non-blocking lock on
``<state_dir>/program/sync.lock`` and, when another sync (a scheduled one, or a
node-local timer pointed at the same lock) holds it, returns at once with
``skipped_run: overlap`` rather than queueing behind it. A scheduled run on a node
with no program data file is a quiet no-op that records nothing.

**Publish after a change.** With ``publish`` set (the scheduled envelope sets
it), a sync that changed something chains into a publish skill through
``invoke_capability``, inside the same lock, when ``program.publish.wiki`` is
declared and ``program.publish.after_sync`` is ``true`` (default off). The
skill is ``program.publish`` unless ``program.publish.after_sync_skill`` names
another registered publisher, so the route can move without code. An unchanged
sync publishes nothing. A publish failure never fails or rolls back the sync:
it is recorded beside the freshness record and reported in the result's
``publish`` block and in every status read's ``freshness.publish``.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.infra.state import LockedJsonFile

from .. import freshness as fr
from ..model import ProgramData, ProgramError, load_program, save_program
from . import _changelog as cl
from ._source import BAD_REQUEST, NO_DATA, resolve_data_path
from ._writer import declared_writer, read_only_reason
from .sources import SOURCE_KINDS, FileSource, ProgramSource, build_source


def _refuse(kind: str, message: str) -> SkillResult:
    return SkillResult(ok=False, value={"refused": kind}, errors=[message])


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _declared_feeders(target: Path) -> list[str]:
    """The live feeders the data file declares in ``program.feeders``.

    Read straight from JSON (no validation) so peeking at the config never
    turns a drift-only file into a refusal. Only known live kinds survive.
    """
    try:
        doc = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    feeders = (doc.get("program") or {}).get("feeders")
    if not isinstance(feeders, list):
        return []
    return [f for f in feeders if f in SOURCE_KINDS and f not in ("file", "all")]


def _inferred_feeders(target: Path) -> list[str]:
    """When ``source=all`` and nothing is declared, infer feeders from the
    tracker host and whether mirror pairs are declared."""
    try:
        doc = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    program = doc.get("program") or {}
    kinds: list[str] = []
    tracker_kind = (program.get("tracker") or {}).get("kind")
    if tracker_kind in ("gitlab", "github"):
        kinds.append(tracker_kind)
    if program.get("mirrors") and "github" not in kinds:
        kinds.append("github")
    return kinds


def _resolve_sources(
    params: dict[str, Any], ctx: SkillContext, target: Path
) -> tuple[list[ProgramSource] | None, str | None]:
    """The source(s) this sync reconciles from, or a refusal message."""
    injected = params.get("_sources")
    if injected is not None:
        return list(injected), None

    source_param = params.get("source")
    if source_param:
        # An explicit upstream file must be strictly valid — it may be copied
        # into data.json, which every reader must accept.
        return [FileSource(source_param, require_listed_owners=True)], None

    kind = params.get("source_kind")
    if kind is not None and kind not in SOURCE_KINDS:
        return None, f"unknown source_kind {kind!r}; one of {', '.join(SOURCE_KINDS)}"

    if kind in ("gitlab", "github"):
        return [build_source(kind, target, state_dir=ctx.state_dir, registry=ctx.registry)], None

    if kind == "all":
        feeders = _declared_feeders(target) or _inferred_feeders(target)
        if feeders:
            return [build_source(k, target, state_dir=ctx.state_dir, registry=ctx.registry) for k in feeders], None
        # nothing live configured → a safe self-reconcile
        return [FileSource(target, require_listed_owners=False)], None

    # kind is None or "file": the configured feeders, else self-reconcile.
    if kind is None:
        feeders = _declared_feeders(target)
        if feeders:
            return [build_source(k, target, state_dir=ctx.state_dir, registry=ctx.registry) for k in feeders], None
    return [FileSource(target, require_listed_owners=False)], None


def _reconcile_one(
    source: ProgramSource,
    target: Path,
    ctx: SkillContext,
    ts: str,
    *,
    read_only: bool = False,
) -> dict[str, Any]:
    """One source's reconcile pass. Returns a per-source summary; never raises
    for a connector problem — an unverified or unreadable source is reported,
    not crashed through. ``read_only`` (this install is not the program's
    writer) computes the same diff and writes nothing."""
    summary: dict[str, Any] = {
        "source": source.origin,
        "verified": True,
        "updated": False,
        "recorded": [],
        "data": None,
        "skipped": None,
        "readback_ok": False,
    }

    readiness = source.verify()
    if not readiness.verified:
        summary["verified"] = False
        summary["skipped"] = {
            "source": source.origin,
            "reason": readiness.detail or "connector unverified",
            "failed_rung": readiness.failed_rung,
        }
        return summary

    # A ProgramError here (invalid source data) propagates: ``run`` turns it
    # into a typed ``no_data`` refusal for a single source — an invalid
    # upstream must never overwrite good state — and a loud skip under
    # ``source=all``. Being *unverified* is the skip case (handled above);
    # producing *invalid data* is the refuse case.
    source_data = source.load()

    if source_data is None:
        summary["note"] = f"source {source.origin} produced no program data; nothing to reconcile"
        return summary

    summary["data"] = source_data

    if read_only:
        old_snapshot: Any = {}
        if cl.snapshot_path(ctx).exists():
            with LockedJsonFile(cl.snapshot_path(ctx)) as snap:
                old_snapshot = snap.read()
        if not isinstance(old_snapshot, dict):
            old_snapshot = {}
        summary["recorded"] = [
            {"ts": ts, "source": source.origin, **change}
            for change in cl.diff_snapshots(old_snapshot, cl.snapshot_of(source_data))
        ]
        summary["readback_ok"] = True
        return summary

    # Write-back when a *distinct* source's content differs (the self-reconcile
    # source IS the file, so nothing is rewritten).
    if source.origin != f"file:{target}":
        current = None
        if Path(target).exists():
            try:
                current = json.loads(Path(target).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = None
        if current != source_data.raw:
            save_program(source_data, target)
            summary["updated"] = True

    # Diff + append under one exclusive lock on the snapshot.
    recorded: list[dict[str, Any]] = []
    with LockedJsonFile(cl.snapshot_path(ctx), exclusive=True) as snap:
        old_snapshot = snap.read()
        if not isinstance(old_snapshot, dict):
            old_snapshot = {}
        new_snapshot = cl.snapshot_of(source_data)
        raw_changes = cl.diff_snapshots(old_snapshot, new_snapshot)
        changelog = cl.changelog_path(ctx)
        base_seq = len(cl.read_changelog(changelog))
        for offset, change in enumerate(raw_changes, start=1):
            record = {"seq": base_seq + offset, "ts": ts, "source": source.origin, **change}
            cite = _evidence_citation(source_data, change)
            if cite is not None:
                record["evidence"] = cite
            cl.append_change(changelog, record)
            recorded.append(record)
        snap.write(new_snapshot)

    summary["recorded"] = recorded
    summary["snapshot"] = new_snapshot
    summary["readback_ok"] = _landed(target, ctx, new_snapshot)
    return summary


def _evidence_citation(data: ProgramData, change: dict[str, Any]) -> dict[str, Any] | None:
    """The citation an evidence-derived write carries into the change log:
    when a ``pct`` / ``delivery`` change matches the item's recorded
    ``progress_basis``, the basis sentence and the evidence links travel with
    the entry, so the log says *why*, not only *what*."""
    if change.get("kind") == "owner_changed":
        item = data.item(str(change.get("subject")))
        basis = (item or {}).get("owner_basis")
        if isinstance(basis, dict) and (item or {}).get("owner") == change.get("new"):
            return {"basis": f"tracker {basis.get('source')}: {basis.get('value')}", "links": []}
        return None
    if change.get("kind") not in ("pct_changed", "delivery_changed"):
        return None
    item = data.item(str(change.get("subject")))
    basis = (item or {}).get("progress_basis")
    entry = basis.get(change.get("field")) if isinstance(basis, dict) else None
    if not isinstance(entry, dict) or entry.get("value") != change.get("new"):
        return None
    return {"basis": entry.get("basis"), "links": list(entry.get("evidence") or [])}


def _landed(target: Path, ctx: SkillContext, expected_snapshot: dict[str, Any]) -> bool:
    """Read back what this sync wrote and confirm it is intact.

    "Saved" is not "stored": the stamp is earned only if the data file still
    parses as a program and the change-detection snapshot on disk is the one
    this pass computed.
    """
    try:
        load_program(target, require_listed_owners=False)
        with LockedJsonFile(cl.snapshot_path(ctx)) as handle:
            return handle.read() == expected_snapshot
    except Exception:  # noqa: BLE001 - anything unreadable means it did not land
        return False


def _record(
    ctx: SkillContext,
    sources: list[ProgramSource],
    *,
    verified: bool,
    reason: str | None,
) -> None:
    """Persist the attempt. A failure to write the record never fails the sync;
    it can only mean the stamp does not advance, which is the safe direction."""
    origins = [s.origin for s in sources]
    live = any(not origin.startswith("file:") for origin in origins)
    try:
        fr.record_attempt(ctx.state_dir, verified=verified, live=live, feeders=origins, reason=reason)
    except Exception:  # noqa: BLE001
        ctx.logger.warning("program sync: could not persist the freshness record", exc_info=True)


def _read_only_result(
    params: dict[str, Any],
    sources: list[ProgramSource],
    read_only: dict[str, Any],
    would_change: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    per_source: list[dict[str, Any]],
    notes: list[str],
) -> SkillResult:
    """What a read-only sync says: what it read and would change, and that it
    wrote nothing (no data file, change log, snapshot or freshness stamp)."""
    value: dict[str, Any] = {
        "source": sources[0].origin if len(sources) == 1 else (params.get("source_kind") or "all"),
        "read_only": read_only,
        "data_updated": False,
        "changes": [],
        "count": 0,
        "would_change": would_change,
        "skipped": skipped,
        "sources": per_source,
        "verified": False,
    }
    if notes:
        value["note"] = "; ".join(notes)
    return SkillResult(ok=True, value=value)


LOCK_NAME = "sync.lock"
PUBLISH_SKILL = "program.publish"


def lock_path(ctx: SkillContext) -> Path:
    """The single-flight lock a scheduled sync (and a node-local timer) shares."""
    return Path(ctx.state_dir) / "program" / LOCK_NAME


@contextlib.contextmanager
def single_flight(path: Path) -> Iterator[bool]:
    """Hold an exclusive, non-blocking ``flock`` on ``path``; yields whether it was taken.

    ``flock(1)`` and this share one lock, so a node-local timer that wraps
    ``program sync`` in ``flock <path>`` and the scheduled run never overlap.
    Where ``fcntl`` is unavailable the run proceeds (the PULSE lease still
    keeps one scheduler leader per node).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield True
        return
    handle = open(path, "a+")  # noqa: SIM115 - held for the whole run
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _publish_target(target: Path) -> tuple[str | None, str | None]:
    """``(skill, reason)``: the publisher to chain into, or why there is none.

    Off unless ``program.publish.after_sync`` is ``true`` and a wiki target is
    declared."""
    try:
        doc = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "the data file is unreadable"
    publish = ((doc.get("program") or {}).get("publish")) if isinstance(doc, dict) else None
    if not isinstance(publish, dict) or publish.get("after_sync") is not True:
        return None, "program.publish.after_sync is not enabled"
    if not isinstance(publish.get("wiki"), dict):
        return None, "no program.publish.wiki target is declared"
    skill = publish.get("after_sync_skill")
    return (skill if isinstance(skill, str) and skill else PUBLISH_SKILL), None


def _chain_publish(ctx: SkillContext, value: dict[str, Any], target: Path) -> dict[str, Any]:
    """Run ``program.publish`` after a sync that changed something. Never raises."""
    if not (value.get("data_updated") or value.get("count")):
        return {"ran": False, "reason": "nothing changed"}
    skill, reason = _publish_target(target)
    if skill is None:
        return {"ran": False, "reason": reason}
    from axiom.infra.skill_dispatch import RUNNER_SURFACE, invoke_capability

    try:
        if ctx.registry is None or not ctx.registry.has(skill):
            raise LookupError(f"{skill} is not registered here")
        result = invoke_capability(ctx.registry, skill, {}, ctx, surface=RUNNER_SURFACE)
        ok = bool(result.ok)
        out = result.value if isinstance(result.value, dict) else {}
        detail = None if ok else ("; ".join(result.errors) or "publish failed")
        block = {"ran": True, "skill": skill, "ok": ok, "committed": bool(out.get("committed")), "error": detail}
    except Exception as exc:  # noqa: BLE001 - a publish must never fail the sync
        block = {"ran": True, "skill": skill, "ok": False, "committed": False,
                 "error": f"publish raised {type(exc).__name__}: {exc}"}
    try:
        fr.record_publish(ctx.state_dir, ok=block["ok"], committed=block["committed"], detail=block["error"])
    except Exception:  # noqa: BLE001
        ctx.logger.warning("program sync: could not persist the publish record", exc_info=True)
    if not block["ok"]:
        ctx.logger.warning("program sync: chained publish failed: %s", block["error"])
    return block


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Reconcile; on the schedule, single-flight and followed by a publish when it changed."""
    if not params.get("single_flight"):
        return _run_and_publish(params, ctx)
    target, _ = resolve_data_path(params, ctx)
    if params.get("scheduled") and target is not None and not Path(target).exists():
        return SkillResult(ok=True, value={"skipped_run": "no_program", "note": "no program data file on this node"})
    with single_flight(lock_path(ctx)) as taken:
        if not taken:
            return SkillResult(
                ok=True,
                value={"skipped_run": "overlap", "note": "another program sync holds the lock; this run did nothing"},
            )
        result = _run_and_publish(params, ctx)
    if params.get("scheduled") and isinstance(result.value, dict):
        # The schedule is the program's own to tune (program.cadence); the host
        # registered the declared defaults and knows nothing more about it.
        from .. import cadence

        result.value["cadence"] = cadence.retune(ctx.state_dir)
    return result


def _run_and_publish(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    result = _reconcile(params, ctx)
    if not params.get("publish") or not result.ok or not isinstance(result.value, dict):
        return result
    if result.value.get("read_only"):
        return result
    target, _ = resolve_data_path(params, ctx)
    block = _chain_publish(ctx, result.value, Path(target)) if target is not None else {"ran": False}
    result.value["publish"] = block
    if block.get("ran"):
        result.actions_taken = [*result.actions_taken, "published" if block.get("ok") else "publish failed"]
    return result


def _reconcile(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    target, refusal = resolve_data_path(params, ctx)
    if refusal is not None:
        return _refuse(BAD_REQUEST, refusal)
    assert target is not None

    sources, src_refusal = _resolve_sources(params, ctx, target)
    if src_refusal is not None:
        return _refuse(BAD_REQUEST, src_refusal)
    assert sources is not None

    read_only = read_only_reason(declared_writer(target), params)
    if read_only is not None:
        for source in sources:
            if hasattr(source, "persist_cursors"):
                source.persist_cursors = False

    ts = _now_iso()
    all_changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    per_source: list[dict[str, Any]] = []
    data_updated = False
    last_data: ProgramData | None = None
    last_snapshot: dict[str, Any] | None = None
    notes: list[str] = []
    _failed: list[str] = []
    all_landed = True

    for source in sources:
        try:
            summary = _reconcile_one(source, target, ctx, ts, read_only=read_only is not None)
        except ProgramError as exc:
            # The single explicit-upstream/self-reconcile path keeps its typed
            # no_data refusal (an invalid upstream must not overwrite state).
            _failed.append(f"{source.origin}: {exc}")
            if len(sources) == 1:
                if read_only is None:
                    _record(ctx, sources, verified=False, reason="; ".join(_failed))
                return _refuse(NO_DATA, str(exc))
            skipped.append({"source": source.origin, "reason": str(exc), "failed_rung": None})
            continue
        except Exception as exc:
            if read_only is None:
                _record(ctx, sources, verified=False, reason=f"{source.origin}: {type(exc).__name__}")
            raise

        per_source.append(
            {
                "source": summary["source"],
                "verified": summary["verified"],
                "updated": summary["updated"],
                "count": len(summary["recorded"]),
                "skipped": summary["skipped"] is not None,
            }
        )
        if summary["skipped"] is not None:
            skipped.append(summary["skipped"])
            continue
        all_landed = all_landed and summary["readback_ok"]
        if summary.get("note"):
            notes.append(summary["note"])
        all_changes.extend(summary["recorded"])
        data_updated = data_updated or summary["updated"]
        if summary["data"] is not None:
            last_data = summary["data"]
        if summary.get("snapshot") is not None:
            last_snapshot = summary["snapshot"]

    if read_only is not None:
        return _read_only_result(params, sources, read_only, all_changes, skipped, per_source, notes)

    # A single source that produced nothing (missing file) keeps the phase-3
    # "nothing to reconcile" shape so the verb is always a safe no-op.
    if last_data is None and not skipped and len(sources) == 1:
        _record(ctx, sources, verified=False, reason=f"{sources[0].origin} produced no program data")
        return SkillResult(
            ok=True,
            value={
                "source": sources[0].origin,
                "data_updated": False,
                "changes": [],
                "count": 0,
                "skipped": [],
                "note": notes[0] if notes else "nothing to reconcile",
            },
        )

    # The stamp is earned, not assumed: every source reconciled (none skipped,
    # none refused), each read to completion, and what was written reads back.
    verified = (
        last_data is not None
        and not skipped
        and len(per_source) == len(sources)
        and all_landed
    )
    reasons = [f"{s['source']}: {s['reason']}" + (f" (failed at {s['failed_rung']})" if s.get("failed_rung") else "") for s in skipped]
    if not reasons and not verified:
        reasons = ["what the sync wrote did not read back intact"]
    _record(ctx, sources, verified=verified, reason="; ".join(reasons) or None)

    program = last_data.program if last_data is not None else {}
    value: dict[str, Any] = {
        "program": (
            {"id": program.get("id"), "name": program.get("name"), "as_of": program.get("as_of")}
            if last_data is not None
            else None
        ),
        "source": sources[0].origin if len(sources) == 1 else (params.get("source_kind") or "all"),
        "data_updated": data_updated,
        "changes": all_changes,
        "count": len(all_changes),
        "skipped": skipped,
        "sources": per_source,
        "verified": verified,
        "freshness": fr.current(ctx.state_dir, program),
    }
    if last_snapshot is not None:
        value["snapshot"] = {
            "items": len(last_snapshot.get("items", {})),
            "lanes": len(last_snapshot.get("lanes", [])),
            "drift": len(last_snapshot.get("drift", [])),
        }
    if notes:
        value["note"] = "; ".join(notes)

    actions = [f"appended {len(all_changes)} change(s)"] if all_changes else []
    if skipped:
        actions.append(f"skipped {len(skipped)} unverified source(s)")
    return SkillResult(ok=True, value=value, actions_taken=actions)
