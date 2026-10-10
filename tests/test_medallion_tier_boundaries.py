# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-128: bronze receives, silver transforms, gold publishes — checked.

The tiers had drifted. Two serving paths — the ingest summary a partner
calls to ask "did my data land?", and the freshness report — read
`silver.signals` directly, reaching past the published surface into the
intermediate one.

The freshness one was worse than a layering slip. `gold.ingest_freshness`
already computes freshness, and computes it BETTER: it carries
`typical_gap`, the feed's own observed cadence, so `gold.ingest_stale`
can flag a lag beyond four times it with no threshold for anyone to
declare. The skill re-derived a cruder version in Python and refused to
grade anything undeclared — which is how SENNA sat four months stale under a
report working exactly as designed.

Two implementations of one rule drift, and here the one nobody was using
was the better of the two.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from axiom.extensions.builtins.data_platform.tiers import (
    ALLOWANCES,
    TIERS,
    WORKING_TIERS,
)

#: The whole of ``src/axiom`` is walked, not the data platform alone. The rule
#: is about the SERVING surface, and serving happens in every extension: a chat
#: view, a receipts page and an ingest summary are all serving, and only one of
#: them lives in the data platform. Scanning one extension would have left the
#: largest surface unguarded — and widening the walk found a read immediately.
_DP = "src/axiom/extensions/builtins/data_platform"

#: ADR-128 E4: the modules that legitimately name a working tier because they
#: ARE its implementation — the conform pass that writes it, the schema manager
#: that creates its columns and the gold views over it, the rename migration
#: that must find every tier, and the two modules that hold the rule itself.
#: Paths are repo-relative.
WRITERS = {
    f"{_DP}/conformance/__init__.py",
    # The conform pass's own accounting of what it just wrote. `rebuild()`
    # rescans silver to repair a count after rows are deleted outside conform,
    # which is the tier's implementation repairing itself rather than a serving
    # surface reading past gold.
    f"{_DP}/conformance/catalogue.py",
    f"{_DP}/skills/conform_run.py",
    f"{_DP}/skills/conform_try.py",
    f"{_DP}/skills/ensure_schema.py",
    # The schema manager for a time-partitioned silver (hypertable, compression,
    # retention): it converts the tier in place, so it must name it.
    f"{_DP}/conformance/timeseries.py",
    # Reconcile (ADR-180 §5) compares two copies of a site's silver and fills
    # one from the other: the tier repairing itself, not a serving read.
    f"{_DP}/conformance/reconcile.py",
    f"{_DP}/skills/register.py",
    f"{_DP}/skills/site_rename.py",
    f"{_DP}/runs/promote.py",
    f"{_DP}/cli.py",
    f"{_DP}/axiom-extension.toml",
    f"{_DP}/tiers.py",
    f"{_DP}/skills/tier_audit.py",
}

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "axiom"

#: A READ of a working tier: `FROM silver.x` / `JOIN bronze.y`.
#:
#: Both working tiers, because the rule is about the serving surface rather
#: than about silver specifically. Bronze has nothing reading it today, which
#: is the moment to write the guard: a rule added after the first violation
#: arrives has to argue with it.
#:
#: Reads only. Writing to silver is what the conform pass and the run promoter
#: are FOR, and `INSERT INTO silver.run_segments` is one of them doing its job;
#: writes are covered separately below, by declaration rather than by refusal.
#: Matching reads only also keeps prose out — a description saying "upsert
#: canonical rows into silver.signals" is documentation, not a query.
#: The trailing ``\w`` is what keeps English out. A sentence ends "derive it
#: from silver." with nothing after the dot; a query never does, because in
#: SQL the dot is always followed by a table name.
_QUERY = re.compile(
    r"\b(from|join)\s+(" + "|".join(WORKING_TIERS) + r")\.\w", re.IGNORECASE
)

#: A WRITE to a tier from outside the conform pass. Not refused — E1, E2 and
#: E3 are real and will be used — but required to say which allowance it is
#: claiming, so a reviewer reads the claim instead of inferring it.
_WRITE = re.compile(
    r"\b(insert\s+into|update|delete\s+from|truncate)\s+(" + "|".join(TIERS) + r")\.",
    re.IGNORECASE,
)

#: `ADR-128 E2` — the marker a write carries. Looked for in the 12 lines above
#: the statement, which is where a module-level SQL constant keeps its comment.
_ALLOWANCE = re.compile(r"ADR-128\s+(E[1-5])")

#: The conform pass IS the rule, so it claims nothing. Everything else that
#: writes a tier declares.
_THE_CONFORM_PASS = f"{_DP}/conformance/__init__.py"


def _modules() -> list[tuple[str, pathlib.Path]]:
    out = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if "/tests/" in f"/{relative}":
            continue
        out.append((relative, path))
    return out


def _reading_modules() -> list[tuple[str, pathlib.Path]]:
    return [(r, p) for r, p in _modules() if r not in WRITERS]


class TestNoServingPathReadsTheWorkingTiers:
    def test_no_module_queries_a_working_tier_except_the_writers(self):
        """A serving read belongs on gold. The exemptions are ADR-128 E4:
        the conform pass, the schema manager and the rename migration —
        everything that IS a tier's implementation rather than its
        consumer."""
        offenders = []
        for relative, path in _reading_modules():
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            for match in _QUERY.finditer(text):
                line = text[: match.start()].count("\n") + 1
                context = "\n".join(lines[max(0, line - 13) : line])
                # E5: a diagnostic may read any tier, provided it SAYS which
                # tier it read. The marker is that declaration.
                if _ALLOWANCE.search(context):
                    continue
                offenders.append(f"{relative}:{line}")
        assert not offenders, (
            "these query a working tier directly; serve from gold, or declare "
            "an ADR-128 E5 diagnostic read naming the tier: " + ", ".join(offenders)
        )

    def test_the_exemptions_are_all_real(self):
        """A stale exemption is how a rule quietly stops covering the thing
        it was written for."""
        missing = [name for name in WRITERS if not (ROOT / name).exists()]
        assert not missing, f"exemptions naming files that are gone: {missing}"

    @pytest.mark.parametrize(
        "module, table",
        [
            (f"{_DP}/ingest_sink/summary.py", "gold.signals"),
            (f"{_DP}/skills/ingest_freshness.py", "gold.ingest_freshness"),
        ],
    )
    def test_the_repointed_readers_name_the_gold_object(self, module, table):
        assert table in (ROOT / module).read_text(encoding="utf-8")


class TestFreshnessUsesTheCadenceGoldMeasured:
    def test_it_selects_the_observed_cadence(self):
        """`typical_gap` is the whole reason to read the view rather than
        re-derive it: it is the feed's own cadence, measured."""
        source = (ROOT / _DP / "skills/ingest_freshness.py").read_text(encoding="utf-8")
        assert "typical_gap" in source

    def test_an_undeclared_stream_is_still_graded(self):
        """The old rule refused to grade without a declaration, which is
        how a four-month silence went unreported."""
        source = (ROOT / _DP / "skills/ingest_freshness.py").read_text(encoding="utf-8")
        assert "observed cadence" in source


class TestEveryTierWriteDeclaresItsAllowance:
    """A write outside the conform pass names the allowance it claims.

    Refusing the write would be wrong — E1, E2 and E3 are legitimate and the
    run promoter is one of them. What was missing is that nothing said so, so
    the next write to silver looked exactly as justified as this one and
    nobody could tell them apart without reading both.
    """

    def _writes(self):
        out = []
        for relative, path in _modules():
            if relative == _THE_CONFORM_PASS:
                continue
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            for match in _WRITE.finditer(text):
                line = text[: match.start()].count("\n") + 1
                out.append((relative, line, "\n".join(lines[max(0, line - 13) : line])))
        return out

    def test_every_write_names_an_allowance(self):
        undeclared = [
            f"{relative}:{line}"
            for relative, line, context in self._writes()
            if not _ALLOWANCE.search(context)
        ]
        assert not undeclared, (
            "these write a medallion tier without naming the ADR-128 allowance "
            "they claim (E1 authored reference, E2 curator annotation, "
            "E3 analysis output): " + ", ".join(undeclared)
        )

    def test_the_allowances_named_are_ones_that_exist(self):
        """A marker reading `E7` is worse than no marker: it looks like a
        claim that was checked."""
        bad = []
        for relative, line, context in self._writes():
            for code in _ALLOWANCE.findall(context):
                if code not in ALLOWANCES:
                    bad.append(f"{relative}:{line} claims {code}")
        assert not bad, f"unknown allowances: {bad}"

    def test_the_run_promoter_is_the_worked_example(self):
        """Not a tautology over the regex: the promoter is the one write the
        rule was written against, so if the marker ever stops being found
        there the guard has stopped covering anything."""
        found = {
            relative for relative, _, context in self._writes() if _ALLOWANCE.search(context)
        }
        assert f"{_DP}/runs/promote.py" in found


class TestTheGuardCanFail:
    """Both halves, proven against text rather than trusted.

    A guard nobody has watched fail is a guard nobody knows the shape of.
    """

    def test_a_read_of_a_working_tier_is_caught(self):
        for tier in WORKING_TIERS:
            assert _QUERY.search(f"SELECT 1 FROM {tier}.signals")
            assert _QUERY.search(f"... JOIN {tier}.runs r ON ...")

    def test_prose_about_a_tier_is_not_caught(self):
        assert not _QUERY.search("upsert canonical rows into silver.signals")
        assert not _QUERY.search("# silver.signals holds the conformed rows")
        # The one this actually caught, in tiers.py's own advice string.
        assert not _QUERY.search("or derive it from silver.")

    def test_an_undeclared_write_is_caught_and_a_declared_one_is_not(self):
        statement = "INSERT INTO gold.rod_calibration (site) VALUES (%(site)s)"
        assert _WRITE.search(statement)
        assert not _ALLOWANCE.search(statement)
        assert _ALLOWANCE.search("# ADR-128 E1 — authored reference data\n" + statement)
