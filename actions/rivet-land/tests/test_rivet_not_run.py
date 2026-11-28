"""When no tests have run, the note says so above any count and never prints a pass count (#2354).

  NR-1  The status strip for a head whose required checks have not run reads "tests: not run"
        in the bad tone and prints no "N passed" text, in the image or its alt text.
  NR-2  The note leads with a caution naming the checks and the exact fix, above the verdict.
  NR-3  A head with no such state renders exactly as before.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import rivet_charts  # noqa: E402
import rivet_note  # noqa: E402

NOW = datetime(2026, 11, 24, 19, 40, tzinfo=UTC)
SENTENCE = "tests: not run. Guards, Lint last ran while the pull request was a draft."
FIX = "Wait for the run that starts when it was marked ready."


def _facts(**kw):
    base = dict(repo="o/r", number=7, now=NOW, passed=1, running=0)
    base.update(kw)
    return rivet_note.Facts(**base)


class _Verdict:
    state = "checks"
    headline = "RIVET: Waiting for its checks to finish."
    why = ()
    who = "nobody"
    next_steps = ("Nothing yet.",)


def test_nr1_the_strip_says_tests_not_run_and_prints_no_pass_count():
    svg = rivet_charts.status_strip_svg(pr=7, word="Tests not run", tone="bad", passed=1, failing=0, running=0, not_run=True)
    assert "tests: not run" in svg
    assert "passed" not in svg and "1 passed" not in svg
    assert "f-bad" in svg


def test_nr1_the_chart_alt_text_prints_no_pass_count():
    seen = {}

    def publish(repo, path, svg):
        seen["svg"] = svg
        return "https://example.test/x.svg"

    block = rivet_charts.chart_block(
        repo="o/r", pr=7, head_sha="a" * 40, group="checks", passed=1, failing=0, running=0,
        publish_fn=publish, not_run=True,
    )
    assert "tests: not run" in block
    assert "passed" not in block


def test_nr2_the_note_leads_with_the_caution_naming_the_checks_and_the_fix():
    body = rivet_note.render(_Verdict(), _facts(tests_not_run=SENTENCE, tests_not_run_fix=FIX))
    assert body.index("tests: not run") < body.index("Its checks are still running")
    assert "[!CAUTION]" in body
    assert "Guards, Lint" in body and FIX in body


def test_nr3_a_head_with_no_such_state_renders_as_before():
    body = rivet_note.render(_Verdict(), _facts())
    assert "tests: not run" not in body and "[!CAUTION]" not in body
    svg = rivet_charts.status_strip_svg(pr=7, word="Waiting", tone="none", passed=1, failing=0, running=0)
    assert "1 passed" in svg
