# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""P14 arming gate — failure injection.

The second third of the arming gate (docs/working/program-phase7-plus-plan.md
§P14). A feeder that is unreachable, times out, or returns partial / malformed
data must degrade gracefully and **never corrupt the authoritative data file**;
a sync interrupted mid-write must leave **no torn state**; a crash + restart
must give **exactly-once** on the change log; and two concurrent sync runs must
**not clobber** each other.

These exercise the real write core — ``model.save_program`` (atomic replace),
``_mutate.commit`` / ``sync`` (the locked snapshot-and-log section), and the
``axiom.infra.state`` locks — with faults injected at the OS / source seam, no
mocks of the subject under test.

One real crash-consistency gap is pinned here as an ``xfail`` (see
``TestCrashBetweenLogAndSnapshot``): a hard crash in the narrow window between
appending the change log and advancing the snapshot double-logs the batch on
restart. It is a low-severity gap in an advisory history log (the authoritative
``data.json`` stays correct) and the correct fix is more than a tidy change, so
it is documented rather than forced. See that class's docstring.
"""

from __future__ import annotations

import copy
import json
import logging
import threading

import pytest

from axiom.extensions.builtins.program import model
from axiom.extensions.builtins.program.model import ProgramData, load_program, save_program
from axiom.extensions.builtins.program.skills import _changelog as cl
from axiom.extensions.builtins.program.skills import sync
from axiom.extensions.builtins.program.skills.sources import ConnectorReadiness
from axiom.infra.skills import SkillContext, SkillRegistry


def _mk_ctx(state):
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.gate.failure"),
        user_prompt=None,
        surface="cli",
    )


@pytest.fixture
def node(tmp_path, data_dict):
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    (state / "program" / "data.json").write_text(json.dumps(data_dict, indent=1), encoding="utf-8")
    return _mk_ctx(state)


def _data_path(node):
    return node.state_dir / "program" / "data.json"


class FakeSource:
    """A pre-built ProgramSource injected via params['_sources']."""

    def __init__(self, origin, *, data=None, readiness=None, raises=None):
        self.origin = origin
        self._data = data
        self._readiness = readiness
        self._raises = raises

    def verify(self):
        return self._readiness or ConnectorReadiness(self.origin, True, True, True, "ok")

    def load(self):
        if self._raises is not None:
            raise self._raises
        return self._data


# ---------------------------------------------------------------------------
# a feeder fault never corrupts the data file
# ---------------------------------------------------------------------------


class TestFeederFaultsDegradeGracefully:
    def test_a_source_that_raises_mid_load_is_a_typed_refusal_not_a_crash(self, node):
        """A feeder that errors or times out mid-read (here a raised
        ``ProgramError``) becomes a typed ``no_data`` refusal — not an
        unhandled crash — and the data file is untouched."""
        from axiom.extensions.builtins.program.model import ProgramError

        before = _data_path(node).read_bytes()
        src = FakeSource("gitlab:x", raises=ProgramError("read timed out"))
        result = sync.run({"_sources": [src]}, node)
        assert not result.ok
        assert result.value["refused"] == "no_data"
        assert _data_path(node).read_bytes() == before
        assert cl.read_changelog(cl.changelog_path(node)) == []

    def test_one_broken_feeder_does_not_stop_the_others(self, node):
        """Under ``source=all`` fan-out, a feeder that raises is recorded in
        ``skipped`` while the healthy feeder still reconciles. One source's
        failure never silences the rest."""
        from axiom.extensions.builtins.program.model import ProgramError

        broken = FakeSource("gitlab:down", raises=ProgramError("connection reset"))
        raw = json.loads(_data_path(node).read_text(encoding="utf-8"))
        raw["capture"] = {
            "source": "github:mirror",
            "system": "github",
            "verified": True,
            "findings": [{"kind": "mirror_gap", "subject": "r->r", "detail": "x", "missing": 1}],
        }
        healthy = FakeSource("github:mirror", data=ProgramData(raw=raw))

        result = sync.run({"_sources": [broken, healthy]}, node)
        assert result.ok  # the batch did not crash
        assert any(s["source"] == "gitlab:down" for s in result.value["skipped"])
        assert any(
            c["kind"] == "drift_opened" and c["field"] == "mirror_gap"
            for c in result.value["changes"]
        )

    def test_malformed_feeder_data_is_refused_and_never_written(self, node):
        """A feeder that produces a structurally invalid program (an owner that
        is not a principal) is refused on the way out — ``save_program``
        re-validates — and the on-disk file is left exactly as it was."""
        before = _data_path(node).read_bytes()
        raw = json.loads(_data_path(node).read_text(encoding="utf-8"))
        raw["schedule"][0]["owner"] = "not-a-principal"  # fails validation
        src = FakeSource("gitlab:evil", data=ProgramData(raw=raw))
        result = sync.run({"_sources": [src]}, node)
        assert not result.ok
        assert result.value["refused"] == "no_data"
        assert _data_path(node).read_bytes() == before
        assert cl.read_changelog(cl.changelog_path(node)) == []

    def test_a_corrupt_data_file_on_disk_refuses_loudly_not_crashes(self, node):
        """A previously torn / hand-mangled ``data.json`` (invalid JSON) makes
        the self-reconcile refuse with a typed ``no_data``, never an unhandled
        traceback — the convergence target is reported broken, loudly."""
        _data_path(node).write_text('{"schema": "axiom.program/0.1", "progr', encoding="utf-8")
        result = sync.run({}, node)
        assert not result.ok
        assert result.value["refused"] == "no_data"


# ---------------------------------------------------------------------------
# the authoritative write is atomic (no torn data.json)
# ---------------------------------------------------------------------------


class TestAtomicSave:
    def test_a_failed_replace_leaves_the_original_intact(self, node, monkeypatch):
        """``save_program`` stages to a temp file and ``os.replace``-s it over
        the target. If the replace step fails (a crash at the commit point),
        the original ``data.json`` is byte-for-byte intact and still loads —
        the file is never a truncated half-write. Regression guard for the
        atomic-write fix."""
        before = _data_path(node).read_bytes()
        data = load_program(_data_path(node))
        edited = ProgramData(raw=copy.deepcopy(data.raw))
        edited.raw["schedule"][0]["pct"] = 77  # a valid change

        def boom(src, dst):
            raise OSError("simulated crash at the replace boundary")

        monkeypatch.setattr(model.os, "replace", boom)
        with pytest.raises(OSError):
            save_program(edited, _data_path(node))

        # The original survives untouched and still parses.
        assert _data_path(node).read_bytes() == before
        assert load_program(_data_path(node)).item("i-one")["pct"] == 15
        # No stray temp file was promoted or left behind.
        leftovers = list((node.state_dir / "program").glob("*.tmp"))
        assert leftovers == []

    def test_invalid_data_never_truncates_the_target(self, node):
        """Validation happens before any byte is written, so an attempt to save
        an invalid program raises and leaves the prior file complete."""
        from axiom.extensions.builtins.program.model import ProgramValidationError

        before = _data_path(node).read_bytes()
        data = load_program(_data_path(node))
        broken = ProgramData(raw=copy.deepcopy(data.raw))
        broken.raw["schedule"][0]["pct"] = 999  # out of range
        with pytest.raises(ProgramValidationError):
            save_program(broken, _data_path(node))
        assert _data_path(node).read_bytes() == before

    def test_a_successful_save_is_a_clean_round_trip(self, node):
        """The atomic path still produces a correct, reloadable file."""
        data = load_program(_data_path(node))
        edited = ProgramData(raw=copy.deepcopy(data.raw))
        edited.raw["schedule"][0]["status"] = "committed"
        save_program(edited, _data_path(node))
        assert load_program(_data_path(node)).item("i-one")["status"] == "committed"


# ---------------------------------------------------------------------------
# crash + restart: the recoverable cases are exactly-once
# ---------------------------------------------------------------------------


class TestCrashBeforeAnyWriteIsClean:
    def test_a_crash_before_any_append_leaves_a_clean_slate_to_recover_from(self, node):
        """A fault that strikes before the first change-log append (the source
        read fails) advances nothing — snapshot untouched, log empty — so the
        restart reconcile records the baseline exactly once, no duplicates."""
        from axiom.extensions.builtins.program.model import ProgramError

        crashed = sync.run({"_sources": [FakeSource("gitlab:x", raises=ProgramError("boom"))]}, node)
        assert not crashed.ok
        assert cl.read_changelog(cl.changelog_path(node)) == []

        # Restart with a healthy self-reconcile: the baseline lands once.
        recovered = sync.run({}, node)
        assert recovered.ok
        entries = cl.read_changelog(cl.changelog_path(node))
        seqs = [e["seq"] for e in entries]
        assert seqs == list(range(1, len(seqs) + 1))
        # Idempotent tail: another restart adds nothing.
        assert sync.run({}, node).value["count"] == 0
        assert len(cl.read_changelog(cl.changelog_path(node))) == len(entries)


class TestCrashBetweenLogAndSnapshot:
    """TRIAGED DEFECT (non-blocking) — a hard crash in the window between the
    change-log appends and the snapshot advance double-logs the batch.

    ``sync._reconcile_one`` (and ``_mutate.commit``) append every change to
    ``changelog.jsonl`` and *then* advance ``snapshot.json``, both inside one
    exclusive lock. The two are separate files with no cross-file atomicity, so
    a crash (power loss / SIGKILL) after the appends but before the snapshot
    write leaves the log advanced and the snapshot behind. On restart the diff
    is recomputed against the un-advanced snapshot and the same batch is
    appended a second time with fresh ``seq`` numbers — a double entry.

    Severity is low and the gate's right-sizing says do not force it: the
    authoritative ``data.json`` stays correct, the change log is advisory
    history, and the duplicate is visible. Neither simple reorder fixes it
    (advancing the snapshot first turns double-logging into *lost* entries,
    which is worse for the per-consumer "what changed" read); a correct fix
    needs cross-file atomicity or replay-dedup, which is more than a tidy
    change. Tracked here so a future fix flips this to xpass.
    """

    @pytest.mark.xfail(
        reason="crash between log-append and snapshot-advance double-logs on restart; "
        "fix needs cross-file atomicity (more than a tidy change). See class docstring.",
        strict=True,
    )
    def test_restart_after_a_crash_at_the_snapshot_advance_is_exactly_once(self, node, monkeypatch):
        import axiom.infra.state as state_mod

        orig_write = state_mod.LockedJsonFile.write

        def crash_on_snapshot(self, data):
            if isinstance(data, dict) and str(data.get("schema", "")).startswith(
                "axiom.program.snapshot"
            ):
                raise RuntimeError("crash between log-append and snapshot-advance")
            return orig_write(self, data)

        monkeypatch.setattr(state_mod.LockedJsonFile, "write", crash_on_snapshot)
        with pytest.raises(RuntimeError):
            sync.run({}, node)

        monkeypatch.undo()  # "restart": the snapshot was never advanced
        sync.run({}, node)

        entries = cl.read_changelog(cl.changelog_path(node))

        def ident(e):
            return (e["kind"], e["subject"], e.get("field"), json.dumps(e.get("old")), json.dumps(e.get("new")))

        idents = [ident(e) for e in entries]
        # The exactly-once contract: no change record appears twice.
        assert len(idents) == len(set(idents)), "duplicate change-log entries after crash+restart"


# ---------------------------------------------------------------------------
# two concurrent syncs do not clobber each other (single-flight / safe)
# ---------------------------------------------------------------------------


class TestConcurrentSyncsAreSafe:
    def _run_many(self, state, n):
        barrier = threading.Barrier(n)
        errors: list[BaseException] = []

        def worker():
            try:
                barrier.wait()  # maximize contention on the snapshot lock
                sync.run({}, _mk_ctx(state))
            except BaseException as exc:  # noqa: BLE001 - surfaced to the test
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], errors

    def test_concurrent_first_syncs_log_the_baseline_exactly_once(self, node):
        """Eight sync runs fire at once on a fresh node. The exclusive snapshot
        lock serializes them: exactly one records the baseline; the rest see the
        advanced snapshot and log nothing. No duplicates, no torn log, seqs
        contiguous."""
        self._run_many(node.state_dir, 8)
        entries = cl.read_changelog(cl.changelog_path(node))

        # Every line parsed (no interleaved / torn appends) and seqs are a
        # strict 1..N with no repeats — the single-flight invariant.
        seqs = [e["seq"] for e in entries]
        assert seqs == list(range(1, len(seqs) + 1))
        assert len(seqs) == len(set(seqs))

        # The baseline is present exactly once per subject (no double-count).
        def ident(e):
            return (e["kind"], e["subject"], e.get("field"))

        idents = [ident(e) for e in entries]
        assert len(idents) == len(set(idents))
        # And a subsequent lone sync converges to silence.
        assert sync.run({}, _mk_ctx(node.state_dir)).value["count"] == 0

    def test_concurrent_syncs_over_one_edit_log_it_once(self, node):
        """With a baseline already established, one edit is made and many syncs
        race. The edit is logged exactly once, not once per racing run."""
        sync.run({}, node)
        before = len(cl.read_changelog(cl.changelog_path(node)))
        path = _data_path(node)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["schedule"][0]["owner"] = "@dana:example-org"
        path.write_text(json.dumps(doc, indent=1), encoding="utf-8")

        self._run_many(node.state_dir, 6)

        entries = cl.read_changelog(cl.changelog_path(node))
        owner_changes = [
            e for e in entries if e["kind"] == "owner_changed" and e["subject"] == "i-one"
        ]
        assert len(owner_changes) == 1  # exactly one, despite six racing syncs
        assert len(entries) == before + 1
