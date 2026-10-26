# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The half of ADR-128 enforcement that can see the database.

A static test holds the code to the rule. It cannot hold the DATABASE to it,
and the database is where the drift showed up: eleven base tables in gold that
no platform code creates, and authored reference records of the same kind
sitting in silver in one case and gold in another.
"""

from __future__ import annotations

import pytest

from ..skills import bind, verbs
from ..tiers import (
    ALLOWANCES,
    DECLARATION_ENV,
    GOLD_DERIVATIONS,
    NOT_ALLOWANCES,
    SERVING_TIER,
    SILVER_TRANSFORMS,
    TIERS,
    WORKING_TIERS,
    classify,
    declared_gold_tables,
    reads_a_working_tier,
    transforms_in_views,
    unserved_working_tables,
)


class TestTheRuleIsWrittenDown:
    def test_the_tiers_are_ordered_raw_to_served(self):
        assert TIERS == ("bronze", "silver", "gold")
        assert SERVING_TIER == "gold"
        assert set(WORKING_TIERS) == set(TIERS) - {SERVING_TIER}

    def test_every_allowance_is_keyed_by_its_own_code(self):
        for code, allowance in ALLOWANCES.items():
            assert allowance.code == code

    @pytest.mark.parametrize("code", ["E1", "E2", "E3", "E4", "E5"])
    def test_the_five_allowances_are_present(self, code):
        assert code in ALLOWANCES
        assert ALLOWANCES[code].rule.strip()

    def test_the_arguments_that_are_not_allowances_are_named_too(self):
        """Each was made once, which is why it is listed rather than left to
        be re-made."""
        assert "convenience" in NOT_ALLOWANCES
        assert "performance" in NOT_ALLOWANCES


class TestTransformationLivesInSilver:
    """Bronze receives, silver transforms, gold publishes.

    The list is the definition. "The working tier" is not something anyone
    can check; twelve named transforms are.
    """

    def test_the_twelve_are_named(self):
        names = [name for name, _ in SILVER_TRANSFORMS]
        assert names == [
            "conform",
            "clean",
            "cast",
            "decompose",
            "hydrate",
            "resolve identity",
            "deduplicate",
            "compose",
            "validate",
            "stamp provenance",
            "reconcile",
            "stage",
        ]

    def test_each_one_says_what_it_means(self):
        """A bare verb is a label. The gloss is what makes it usable by
        somebody deciding where a new table goes."""
        for name, gloss in SILVER_TRANSFORMS:
            assert gloss.strip(), name
            assert gloss != name

    def test_no_transform_is_listed_twice(self):
        names = [name for name, _ in SILVER_TRANSFORMS]
        assert len(names) == len(set(names))

    def test_gold_derives_it_does_not_transform(self):
        """Gold's verbs change the SHAPE of an answer. Silver's change what a
        record means. Nothing may appear in both lists, because a verb in both
        is a verb with no home."""
        assert not set(GOLD_DERIVATIONS) & {name for name, _ in SILVER_TRANSFORMS}


class TestClassify:
    def test_an_undeclared_gold_base_table_is_a_finding(self):
        findings = classify([("gold", "rod_calibration", "BASE TABLE")])
        assert [f.name for f in findings] == ["rod_calibration"]
        assert DECLARATION_ENV in findings[0].detail

    def test_a_declared_one_is_not(self):
        findings = classify(
            [("gold", "rod_calibration", "BASE TABLE")],
            declared=frozenset({"rod_calibration"}),
        )
        assert findings == []

    def test_gold_views_are_the_normal_case(self):
        assert classify([("gold", "signals", "VIEW")]) == []

    def test_silver_and_bronze_base_tables_are_the_normal_case(self):
        """The working tiers are MADE of base tables. Flagging them would be
        the check misunderstanding the rule it enforces."""
        assert (
            classify(
                [("silver", "signals", "BASE TABLE"), ("bronze", "ingested_files", "BASE TABLE")]
            )
            == []
        )

    def test_findings_come_back_in_a_stable_order(self):
        """A deploy log that reorders itself run to run cannot be diffed."""
        objects = [
            ("gold", "sample_tracking", "BASE TABLE"),
            ("gold", "core_config", "BASE TABLE"),
        ]
        assert [f.name for f in classify(objects)] == ["core_config", "sample_tracking"]
        assert [f.name for f in classify(list(reversed(objects)))] == [
            "core_config",
            "sample_tracking",
        ]


class TestAGoldViewServingBronze:
    def test_it_is_a_finding(self):
        findings = reads_a_working_tier([("gold", "raw_peek", "bronze", "ingested_files")])
        assert len(findings) == 1
        assert "bronze.ingested_files" in findings[0].detail

    def test_a_gold_view_over_silver_is_the_whole_point_of_the_tier(self):
        assert reads_a_working_tier([("gold", "signals", "silver", "signals")]) == []

    def test_a_silver_view_over_bronze_is_the_conform_tier_doing_its_job(self):
        assert reads_a_working_tier([("silver", "staged", "bronze", "raw")]) == []


class TestAGoldViewThatTransforms:
    """Gold derives; it does not transform.

    Found live, and found three times: `gold.reactor_daily`,
    `gold.reactor_power` and `gold.reactor_status_clean` each divide a watt
    column by a million and call the result megawatts. Three independent
    copies of one conversion, and nothing compares them — which is exactly
    what the rule predicts and why the unit vocabulary belongs in silver.
    """

    W_TO_MW = (
        "round((nm1000_power_w / '1000000'::numeric::double precision)::numeric, 4) "
        "AS power_mw"
    )

    def test_a_unit_conversion_is_found(self):
        findings = transforms_in_views([("gold", "reactor_power", self.W_TO_MW)])
        assert any("unit conversion" in f.detail for f in findings)

    def test_the_finding_quotes_the_fragment(self):
        """A finding a reader has to go and grep for is a finding they will
        not act on."""
        findings = transforms_in_views([("gold", "reactor_power", self.W_TO_MW)])
        assert any("1000000" in f.detail for f in findings)

    def test_an_aggregate_is_derivation_not_transformation(self):
        """`round(avg(x), 3)` changes the shape of an answer, not what a
        record means. Flagging it would make the check unusable."""
        assert transforms_in_views(
            [("gold", "reactor_daily", "round(avg(nm1000_power_w)::numeric, 3) AS avg_power_w")]
        ) == []

    def test_a_pure_projection_is_clean(self):
        assert transforms_in_views(
            [("gold", "signals", "SELECT site, feed, ts, value, unit FROM silver.signals")]
        ) == []

    def test_dividing_by_a_small_number_is_not_a_unit_conversion(self):
        """`(a + b + c + d) / 4.0` is a mean of four rod positions. The
        threshold is three digits because a unit prefix is a power of ten."""
        assert transforms_in_views(
            [("gold", "measured_crh_daily", "(a + b + c + d)::numeric / 4.0 AS rod_avg")]
        ) == []

    def test_a_name_that_says_it_cleans_is_flagged(self):
        findings = transforms_in_views([("gold", "reactor_status_clean", "SELECT 1")])
        assert any("the name says it cleans" in f.detail for f in findings)

    def test_hydration_from_a_literal_is_flagged(self):
        findings = transforms_in_views(
            [("gold", "v", "coalesce(t.model_ref, 'unknown') AS model_ref")]
        )
        assert any("hydration" in f.detail for f in findings)

    def test_silver_views_are_not_examined(self):
        """A silver view transforming is silver doing its job."""
        assert transforms_in_views([("silver", "staged", self.W_TO_MW)]) == []

    def test_one_signature_is_reported_once_per_view(self):
        """Four conversions in one view is one decision to make, not four."""
        definition = self.W_TO_MW + " , " + self.W_TO_MW + " , " + self.W_TO_MW
        findings = transforms_in_views([("gold", "v", definition)])
        assert len([f for f in findings if "unit conversion" in f.detail]) == 1

    def test_an_unreadable_definition_invents_nothing(self):
        """`view_definition` is NULL when the caller cannot see the body.
        Absent evidence is not evidence of absence, and it is certainly not a
        finding."""
        assert transforms_in_views([("gold", "v", "")]) == []


class TestUnservedWorkingTables:
    """The preventive half: which tables a serving path would have to break
    the rule to read."""

    OBJECTS = [
        ("silver", "signals", "BASE TABLE"),
        ("silver", "rod_anomalies", "BASE TABLE"),
        ("silver", "reactor_operator_roster", "BASE TABLE"),
        ("bronze", "ingested_files", "BASE TABLE"),
        ("gold", "signals", "VIEW"),
    ]
    VIEWS = [("gold", "signals", "silver", "signals")]

    def test_it_lists_the_tables_no_gold_object_reads(self):
        got = unserved_working_tables(self.OBJECTS, self.VIEWS)
        assert got == [
            ("bronze", "ingested_files"),
            ("silver", "reactor_operator_roster"),
            ("silver", "rod_anomalies"),
        ]

    def test_a_table_a_gold_view_reads_is_served(self):
        assert ("silver", "signals") not in unserved_working_tables(self.OBJECTS, self.VIEWS)

    def test_a_silver_view_over_it_does_not_count_as_served(self):
        """Serving means gold. A silver view over a silver table is the
        working tier organising itself."""
        views = [("silver", "recent", "silver", "rod_anomalies")]
        assert ("silver", "rod_anomalies") in unserved_working_tables(self.OBJECTS, views)

    def test_gold_base_tables_are_not_listed(self):
        """They are a separate finding, and listing them here as well would
        report one thing twice under two names."""
        objects = [("gold", "rod_calibration", "BASE TABLE")]
        assert unserved_working_tables(objects, []) == []


class TestDeclaration:
    def test_the_default_is_empty_because_axiom_writes_no_gold_tables(self):
        assert declared_gold_tables({}) == frozenset()

    def test_it_reads_a_comma_separated_list(self):
        got = declared_gold_tables({DECLARATION_ENV: "rod_calibration, core_config"})
        assert got == frozenset({"rod_calibration", "core_config"})

    def test_blank_entries_do_not_become_a_table_named_nothing(self):
        assert declared_gold_tables({DECLARATION_ENV: "a,,  ,b"}) == frozenset({"a", "b"})


class TestTheSkillIsReachable:
    """A capability registered without a surface exists at the terminal and
    nowhere else."""

    def test_it_is_a_cli_verb(self):
        assert "tier_audit" in verbs()

    def test_it_registers_with_a_spec_and_is_readable_from_mcp(self):
        from axiom.infra.skills import SkillRegistry

        registry = SkillRegistry()
        bind(registry)
        spec = registry.spec("data.tier_audit")
        assert spec.description
        assert "mcp" in spec.surfaces
        assert spec.side_effects is False

    def test_the_cli_parser_accepts_it(self):
        from ..cli import _parser

        args = _parser().parse_args(["tier-audit", "--schemas", "gold"])
        assert args.verb == "tier-audit"
        assert args.schemas == "gold"

    def test_an_empty_schema_list_is_refused_before_a_connection_is_opened(
        self, tmp_path
    ):
        """Checking the inputs first is why this branch needs no database."""
        import logging

        from axiom.infra.skills import SkillContext, SkillRegistry

        from ..skills import tier_audit

        ctx = SkillContext(
            registry=SkillRegistry(),
            state_dir=str(tmp_path),
            logger=logging.getLogger(__name__),
        )
        result = tier_audit.run({"schemas": " , "}, ctx)
        assert result.ok is False
        assert result.errors == ["schemas is empty"]
