# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The pending-diagnosis banner, and the dismissal it advertises.

Both were reported from live use in one sitting, and they share a shape: each
says something that is not so.

The banner printed the full remedy inline — forty lines of Docker and Postgres
instructions — in front of an unrelated `neut update`. A diagnosis worth
keeping is worth reading on purpose; pasted in front of every command it is
noise that trains people to skip the one that matters.

The dismissal it points at printed "Cleared diagnosis <fp>." unconditionally,
including when it removed nothing. An escape hatch that reports success
without acting is worse than one that fails, because the person walks away
believing the queue is empty.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.diagnostics import cli_listener


@pytest.fixture
def state(tmp_path):
    d = tmp_path / "state"
    (d / "agents" / "triage").mkdir(parents=True)
    return d


def _write(state, *records):
    p = cli_listener.pending_path(state)
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _record(fp="abc123def456", summary="Something went wrong.", remedy="line1\nline2\nline3"):
    return {
        "fingerprint": fp,
        "summary": summary,
        "remedy": remedy,
        "confidence": 0.6,
        "matched_at": "2026-09-04T07:47:23+00:00",
        "pattern_id": "p:1",
    }


class TestClearReportsWhatItActuallyDid:
    def test_clearing_a_present_fingerprint_removes_it_and_says_so(self, state):
        _write(state, _record("aaa"), _record("bbb"))
        removed = cli_listener.clear_pending(state, fingerprint="aaa")
        assert removed == 1
        assert [d["fingerprint"] for d in cli_listener.read_pending(state)] == ["bbb"]

    def test_clearing_an_absent_fingerprint_reports_zero(self, state):
        """The bug, stated as a test.

        `neut triage clear <fp>` printed success twice against a queue it
        never touched. The caller could not tell, because the function
        returned nothing and the command printed the same line either way.
        """
        _write(state, _record("aaa"))
        assert cli_listener.clear_pending(state, fingerprint="nope") == 0
        assert len(cli_listener.read_pending(state)) == 1

    def test_clearing_all_reports_how_many_went(self, state):
        _write(state, _record("aaa"), _record("bbb"), _record("ccc"))
        assert cli_listener.clear_pending(state, fingerprint=None) == 3
        assert cli_listener.read_pending(state) == []

    def test_clearing_an_empty_queue_reports_zero_rather_than_pretending(self, state):
        assert cli_listener.clear_pending(state, fingerprint="anything") == 0

    def test_clearing_the_last_one_empties_the_queue(self, state):
        _write(state, _record("only"))
        assert cli_listener.clear_pending(state, fingerprint="only") == 1
        assert cli_listener.read_pending(state) == []


class TestTheBannerIsReadableInFrontOfAnotherCommand:
    def _banner(self, capsys, monkeypatch, records):
        from axiom.infra import cli_hooks

        monkeypatch.delenv("AXI_DIAGNOSES_QUIET", raising=False)
        monkeypatch.setattr(cli_listener, "read_pending", lambda *a, **k: records)
        monkeypatch.setattr(
            "axiom.extensions.builtins.diagnostics.cli_listener.read_pending",
            lambda *a, **k: records,
        )
        cli_hooks.surface_pending_diagnoses()
        return capsys.readouterr().err

    def test_the_remedy_is_not_pasted_inline(self, capsys, monkeypatch):
        long_remedy = "\n".join(f"step {i}: do a thing" for i in range(30))
        err = self._banner(capsys, monkeypatch, [_record(remedy=long_remedy)])
        assert "step 29" not in err, (
            "a thirty-line remedy in front of an unrelated command is noise; "
            "point at where to read it instead"
        )
        assert err.count("\n") <= 6, f"banner should stay compact, got:\n{err}"

    def test_it_says_the_failure_was_an_earlier_command(self, capsys, monkeypatch):
        err = self._banner(capsys, monkeypatch, [_record()])
        assert "earlier" in err.lower(), (
            "it appears in front of an unrelated command; say so, or it reads "
            "as though the command just run had failed"
        )

    def test_it_names_the_fingerprint_and_how_to_read_the_remedy(self, capsys, monkeypatch):
        err = self._banner(capsys, monkeypatch, [_record(fp="abc123def456")])
        assert "abc123def456" in err
        assert "triage pending" in err, "point at the full remedy"
        assert "triage clear" in err, "point at the dismissal"

    def test_a_long_summary_is_truncated(self, capsys, monkeypatch):
        err = self._banner(capsys, monkeypatch, [_record(summary="x" * 500)])
        assert "x" * 500 not in err
        for line in err.splitlines():
            assert len(line) <= 140, f"line too long ({len(line)}): {line[:80]}"

    def test_several_diagnoses_stay_one_line_each(self, capsys, monkeypatch):
        recs = [_record(fp=f"fp{i:010d}") for i in range(4)]
        err = self._banner(capsys, monkeypatch, recs)
        for r in recs:
            assert r["fingerprint"] in err
        assert err.count("\n") <= 9

    def test_quiet_still_silences_it(self, capsys, monkeypatch):
        from axiom.infra import cli_hooks

        monkeypatch.setenv("AXI_DIAGNOSES_QUIET", "1")
        monkeypatch.setattr(
            "axiom.extensions.builtins.diagnostics.cli_listener.read_pending",
            lambda *a, **k: [_record()],
        )
        cli_hooks.surface_pending_diagnoses()
        assert capsys.readouterr().err == ""


class TestTheCommandItselfTellsTheTruth:
    """The library returning a count is useless if the command ignores it.

    A mutant that made `_cmd_clear` print success unconditionally passed every
    test above, because they all exercise `clear_pending` directly. The command
    is where the operator read "Cleared diagnosis <fp>." against a queue that
    still held it, so the command is what needs pinning.
    """

    def _run(self, monkeypatch, capsys, *, removed: int, fingerprint="abc"):
        import argparse

        from axiom.extensions.builtins.diagnostics import agent_cli

        monkeypatch.setattr(cli_listener, "clear_pending", lambda *a, **k: removed)
        args = argparse.Namespace(clear_all=False, fingerprint=fingerprint)
        code = agent_cli._cmd_clear(args)
        return code, capsys.readouterr().out

    def test_clearing_nothing_does_not_claim_success(self, monkeypatch, capsys):
        code, out = self._run(monkeypatch, capsys, removed=0, fingerprint="ghost")
        assert "Cleared diagnosis" not in out, (
            "this is the reported bug: it printed success against a queue it never touched"
        )
        assert "ghost" in out and "No pending diagnosis" in out
        assert code != 0, "a dismissal that removed nothing is not a success"

    def test_clearing_something_reports_it(self, monkeypatch, capsys):
        code, out = self._run(monkeypatch, capsys, removed=1, fingerprint="abc")
        assert "Cleared diagnosis abc." in out
        assert code == 0

    def test_clear_all_reports_the_count(self, monkeypatch, capsys):
        import argparse

        from axiom.extensions.builtins.diagnostics import agent_cli

        monkeypatch.setattr(cli_listener, "clear_pending", lambda *a, **k: 3)
        code = agent_cli._cmd_clear(argparse.Namespace(clear_all=True, fingerprint=None))
        out = capsys.readouterr().out
        assert "3 pending diagnoses" in out and code == 0

    def test_clear_all_on_an_empty_queue_says_nothing_to_clear(self, monkeypatch, capsys):
        import argparse

        from axiom.extensions.builtins.diagnostics import agent_cli

        monkeypatch.setattr(cli_listener, "clear_pending", lambda *a, **k: 0)
        agent_cli._cmd_clear(argparse.Namespace(clear_all=True, fingerprint=None))
        assert "Nothing to clear" in capsys.readouterr().out
