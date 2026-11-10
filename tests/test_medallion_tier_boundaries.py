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

import ast
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
    # The deposit half of the conform pass (ADR-194): it writes the rows it
    # conforms from mirrored files and keeps silver.conformed_deposits, the
    # content-hash ledger of which bytes already conformed. The tier's own
    # implementation, exactly like conformance/__init__.py beside it.
    f"{_DP}/conformance/deposits.py",
    # Moving silver onto its reading-identity key (ADR-206): preflight counts duplicates, collapse
    # keeps the newest conform and audits the rest, build-index builds the key. The tier's own
    # migration, like the rename migration.
    f"{_DP}/conformance/reading_identity.py",
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

#: ADR-128 E5: the modules allowed to READ a working tier, each with the reason
#: it is a diagnostic or authoring tool rather than a consumer.
#:
#: The ``ADR-128 E5`` comment next to a query used to be enough on its own, and
#: that made the rule self-service: any module, a page or a chat tool included,
#: could paste the marker and read silver. Now the marker is necessary and not
#: sufficient. The module must also be named here, with a reason a reviewer can
#: argue with, so adding a working-tier read to a consumer is a visible edit to
#: this list rather than a comment nobody checks.
#:
#: Gold derivations are not consumers: a view or a materializer that SELECTs from
#: silver is how gold gets built. ``kit/contribute.py`` emits the text of one.
DIAGNOSTIC_READERS = {
    f"{_DP}/duplicate_keys.py": "asks the table whether its own reading-identity key holds",
    f"{_DP}/skills/gaps.py": "data.gaps: asks the working tier whether a site's declarations hold",
    f"{_DP}/kit/runner.py": "kit-try/kit-up: the author's sandbox materializer, run as the tenant role",
    f"{_DP}/kit/check.py": "kit-check: probes tenant isolation with planted foreign rows",
    f"{_DP}/kit/medallion.py": "kit stamp: refuses a database that already holds silver rows",
    f"{_DP}/kit/contribute.py": "emits the text of a gold derivation (gold is built from silver)",
    "src/axiom/extensions/builtins/status/function_status.py": (
        "archive health: oldest day and compression of the silver archive itself"
    ),
    "src/axiom/cli/ext/lifecycle.py": "conform_funnel: did this schema_ref conform? Only silver answers",
}


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
                # E5: a diagnostic may read any tier, provided it is NAMED in
                # DIAGNOSTIC_READERS and SAYS which tier it read. The marker
                # alone is not enough, or any consumer could write one.
                if relative in DIAGNOSTIC_READERS and _ALLOWANCE.search(context):
                    continue
                offenders.append(f"{relative}:{line}")
        assert not offenders, (
            "these query a working tier directly; serve from gold. A diagnostic must "
            "be listed in DIAGNOSTIC_READERS with a reason AND carry an ADR-128 E5 "
            "marker naming the tier: " + ", ".join(offenders)
        )

    def test_the_exemptions_are_all_real(self):
        """A stale exemption is how a rule quietly stops covering the thing
        it was written for."""
        missing = [name for name in WRITERS if not (ROOT / name).exists()]
        assert not missing, f"exemptions naming files that are gone: {missing}"

    def test_every_diagnostic_reader_is_real_and_still_reads(self):
        """A listed diagnostic that stopped reading a tier is an exemption with
        nothing under it, and the next module to need one will copy the entry."""
        stale = []
        for name in DIAGNOSTIC_READERS:
            path = ROOT / name
            if not path.exists() or not _QUERY.search(path.read_text(encoding="utf-8")):
                stale.append(name)
        assert not stale, f"diagnostic readers that no longer read a working tier: {stale}"

    def test_a_diagnostic_is_never_a_consuming_layer(self):
        """Webapp, figure, chat, MCP and lane code serve people and agents. None
        of them may ever be a listed diagnostic."""
        consumers = ("webapp/", "scidisplay/", "chat/", "mcp/", "lane/", "receipts/")
        bad = [n for n in DIAGNOSTIC_READERS if any(f"/builtins/{c}" in n for c in consumers)]
        assert not bad, f"consuming layers listed as diagnostics: {bad}"

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


#: A relation in a string literal: ``silver.signals``, ``"bronze"."x"``. No
#: whitespace around the dot, which is what keeps a sentence ending "in bronze." out.
_RELATION = re.compile(r"\b(?:" + "|".join(WORKING_TIERS) + r")\"?\.\"?[A-Za-z_]", re.IGNORECASE)
#: A literal that IS a tier name: a schema or tier argument (``schema="silver"``,
#: ``tier="bronze"``) names a working tier without ever writing ``FROM``.
_BARE_TIER = re.compile(r"^\s*(?:" + "|".join(WORKING_TIERS) + r")\s*$", re.IGNORECASE)

#: Outside the data platform nothing is a producer, so nothing may name a working
#: tier in a string, whatever the verb. The query regex above only sees
#: ``FROM`` and ``JOIN``; a consumer can reach silver through a schema argument,
#: a ``search_path``, an f-string table name or a ``tier=`` parameter, none of
#: which it matches.
#:
#: Docstrings and comments are prose and are not read. Paths are repo-relative.
NAMES_A_TIER_OUTSIDE_THE_PLATFORM = {
    "src/axiom/infra/tenancy.py": (
        "the tenant reaper's table of schemas to delete a site's rows from, "
        "a retention operation on the tier rather than a read of it"
    ),
    "src/axiom/extensions/builtins/status/function_status.py": "E5 diagnostic, listed above",
    "src/axiom/cli/ext/lifecycle.py": "E5 diagnostic, listed above",
    "src/axiom/extensions/builtins/library/scope.py": (
        "the library's upload store directory: a file the library RECEIVES lands "
        "under library/bronze as the original bytes, the producer side of bronze, "
        "never a read of a medallion table"
    ),
}

#: Inside the data platform, the modules that SERVE: the gold verbs, the ingest
#: summary a partner calls and the freshness report. They have no business
#: naming a working tier at all, so they get the same string rule as everything
#: outside it. Verbs that take a ``tier`` argument on purpose (the catalog,
#: describe, freshness and sample resolvers) are diagnostics and are not here.
SERVING_MODULES = (
    f"{_DP}/skills/gold.py",
    f"{_DP}/skills/ingest_freshness.py",
    f"{_DP}/ingest_sink/summary.py",
    f"{_DP}/ingest_sink/edge_summary.py",
)


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def _tier_literals(source: str) -> list[tuple[int, str]]:
    """Every non-docstring string literal that names a working tier."""
    tree = ast.parse(source)
    docs = _docstring_ids(tree)
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and (_RELATION.search(node.value) or _BARE_TIER.match(node.value))
        ):
            hits.append((node.lineno, node.value[:60]))
    return hits


class TestNoConsumingLayerNamesAWorkingTier:
    def test_nothing_outside_the_platform_names_a_working_tier(self):
        offenders = []
        for relative, path in _modules():
            if f"{_DP}/" in relative or relative in NAMES_A_TIER_OUTSIDE_THE_PLATFORM:
                continue
            for line, text in _tier_literals(path.read_text(encoding="utf-8")):
                offenders.append(f"{relative}:{line} {text!r}")
        assert not offenders, (
            "these name bronze or silver in a string outside the data platform. "
            "Consumers read gold; if gold lacks what you need, add a gold view "
            "(see ADR-128): " + "; ".join(offenders)
        )

    @pytest.mark.parametrize("module", SERVING_MODULES)
    def test_the_serving_modules_name_no_working_tier(self, module):
        hits = _tier_literals((ROOT / module).read_text(encoding="utf-8"))
        assert not hits, f"{module} serves, and names a working tier: {hits}"

    def test_the_named_exemptions_are_real(self):
        missing = [n for n in (*NAMES_A_TIER_OUTSIDE_THE_PLATFORM, *SERVING_MODULES)
                   if not (ROOT / n).exists()]
        assert not missing, f"exemptions naming files that are gone: {missing}"

    def test_the_exemptions_still_name_a_tier(self):
        stale = [
            n for n in NAMES_A_TIER_OUTSIDE_THE_PLATFORM
            if not _tier_literals((ROOT / n).read_text(encoding="utf-8"))
        ]
        assert not stale, f"exemptions with nothing under them: {stale}"


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

    def test_the_marker_alone_no_longer_licenses_a_read(self):
        """The hole this closed: a consumer pasting ``ADR-128 E5`` above its
        query. The marker is found, and the module is still not listed."""
        planted = "# ADR-128 E5: a diagnostic read\nSELECT 1 FROM silver.signals"
        assert _QUERY.search(planted) and _ALLOWANCE.search(planted)
        assert "src/axiom/extensions/builtins/webapp/catalog/series.py" not in DIAGNOSTIC_READERS

    def test_a_read_by_any_route_is_caught_in_a_string(self):
        for source in (
            'q = "SELECT * FROM silver.signals"',
            'q = f"SELECT * FROM {schema}.x WHERE s = \'bronze.x\'"',
            'T = Table("signals", meta, schema="silver")',
            'tier = "bronze"',
            'q = "SELECT * FROM \\"silver\\".\\"signals\\""',
        ):
            assert _tier_literals(source), source

    def test_prose_and_docstrings_are_not_caught_by_the_string_rule(self):
        assert not _tier_literals(
            '"""Module about silver.signals."""\n'
            "def f():\n"
            '    """Reads gold; silver.signals is the source."""\n'
            "    # silver.signals is mentioned in a comment\n"
            '    return "gold.signals"\n'
        )
