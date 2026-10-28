# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Readings that are impossible only in each other's company.

A per-channel scan cannot see this class. `0 degC` is a fine temperature,
`1,170,000 W` is a fine power, and neither is a sentinel, a negative, or a
duplicate — so a scan of 220 channels one at a time passed every one of them.
They contradict each other *at the same instant*, and the signature is the
coincidence, which needs the whole frame.

The numbers below are real, from the 2026-09-28 finding: 5 of 5 readings above
the 1.1 MW licence were taken while a fuel thermocouple read exactly 0 degC, and
the maximum power under a live thermocouple was 1.03 MW.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform import company


def frame(**kw):
    """A frame is what every channel read at one instant."""
    return {k: company.Reading(k, v) for k, v in kw.items()}


class TestTheSignatureItFoundInRealData:
    RULE = company.ZeroWhileCompanionAbove(
        zero=["FuelTemp1", "FuelTemp2"],
        companion=["PoolTemp"],
        above=5.0,
        subject_reason="company.zero_while_companion_warm",
    )

    def test_two_thermocouples_at_zero_in_warm_water_are_flagged(self):
        out = company.judge(frame(FuelTemp1=0.0, FuelTemp2=0.0, PoolTemp=23.0), [self.RULE])
        flagged = {v.channel: v for v in out}
        assert set(flagged) == {"FuelTemp1", "FuelTemp2", "PoolTemp"}

    def test_the_impossible_channel_is_bad_and_its_company_only_suspect(self):
        """The distinction matters. A thermocouple in 23 degC water reading
        exactly 0 is not reporting a temperature — that value is `bad`, and
        ADR-132 says bad means the value goes NULL. The pool reading is not
        known to be wrong; it is only implicated. Marking it `bad` too would
        delete a reading that is probably fine, and marking the thermocouple
        merely `suspect` would leave a fabricated 0 degC in every mean."""
        out = {
            v.channel: v
            for v in company.judge(frame(FuelTemp1=0.0, FuelTemp2=0.0, PoolTemp=23.0), [self.RULE])
        }
        assert out["FuelTemp1"].quality == "bad"
        assert out["FuelTemp2"].quality == "bad"
        assert out["PoolTemp"].quality == "suspect"

    def test_a_cold_pool_does_not_trigger_it(self):
        """Below the companion threshold there is no contradiction: a
        thermocouple can legitimately read 0 in near-freezing water."""
        assert company.judge(frame(FuelTemp1=0.0, FuelTemp2=0.0, PoolTemp=2.0), [self.RULE]) == []

    def test_a_real_warm_reading_does_not_trigger_it(self):
        """At real full power these channels read 305-366 degC."""
        assert (
            company.judge(frame(FuelTemp1=305.0, FuelTemp2=366.0, PoolTemp=23.0), [self.RULE]) == []
        )


class TestExactlyZeroNotNearlyZero:
    RULE = TestTheSignatureItFoundInRealData.RULE

    def test_a_cold_but_live_thermocouple_is_not_a_fault(self):
        """0.3 degC is a cold reading. A dead channel reads EXACTLY 0.0, and
        widening this to "close to zero" would condemn real cold data."""
        assert company.judge(frame(FuelTemp1=0.3, FuelTemp2=0.0, PoolTemp=23.0), [self.RULE]) != []
        out = {
            v.channel: v
            for v in company.judge(frame(FuelTemp1=0.3, FuelTemp2=0.0, PoolTemp=23.0), [self.RULE])
        }
        assert "FuelTemp1" not in out
        assert out["FuelTemp2"].quality == "bad"

    def test_an_absent_reading_is_not_a_zero(self):
        """Absence has kinds. A channel that reported nothing has not reported
        zero, and treating None as 0.0 would invent a fault out of a gap."""
        assert (
            company.judge(frame(FuelTemp1=None, FuelTemp2=None, PoolTemp=23.0), [self.RULE]) == []
        )


class TestPowerWhileEveryRodReadsZero:
    RULE = company.ValueWhileAllZero(
        subject="Power",
        above=1_000.0,
        all_zero=["Tran", "Shim1", "Shim2", "Reg"],
        subject_reason="company.power_while_rods_read_inserted",
    )

    def test_a_megawatt_with_every_rod_at_zero_is_flagged(self):
        """The real event: 1.17 MW while all four rod channels read 0. At true
        full power the same four read 683-739."""
        out = {
            v.channel: v
            for v in company.judge(
                frame(Power=1_170_000.0, Tran=0.0, Shim1=0.0, Shim2=0.0, Reg=0.0), [self.RULE]
            )
        }
        assert out["Power"].quality == "bad"
        assert out["Shim1"].quality == "suspect"

    def test_one_live_rod_channel_is_enough_to_clear_it(self):
        """EVERY named channel must read zero. One plausible rod position means
        the frame is not uniformly zeroed, and the coincidence is gone."""
        assert (
            company.judge(
                frame(Power=1_170_000.0, Tran=0.0, Shim1=712.0, Shim2=0.0, Reg=0.0), [self.RULE]
            )
            == []
        )

    def test_low_power_with_rods_inserted_is_normal(self):
        """Rods in, no power. That is a shut-down reactor, not a fault."""
        assert (
            company.judge(frame(Power=1.8e-4, Tran=0.0, Shim1=0.0, Shim2=0.0, Reg=0.0), [self.RULE])
            == []
        )


class TestMeasuredAgainstItsModelTwin:
    RULE = company.ExceedsTwinBy(
        subject="Power",
        twin="DT_model_power",
        factor=100.0,
        subject_reason="company.measured_exceeds_model_twin",
    )

    def test_six_orders_above_the_sites_own_model_is_flagged(self):
        """The corroborating evidence sits in the same row: the site's own model
        said 1.2e-4 W at the instant the measurement said 1.08 MW."""
        out = {
            v.channel: v
            for v in company.judge(frame(Power=1_080_000.0, DT_model_power=1.2e-4), [self.RULE])
        }
        assert out["Power"].quality == "suspect"

    def test_agreement_is_not_flagged(self):
        assert company.judge(frame(Power=930_000.0, DT_model_power=910_000.0), [self.RULE]) == []

    def test_a_zero_twin_does_not_divide_by_zero(self):
        company.judge(frame(Power=5.0, DT_model_power=0.0), [self.RULE])

    def test_a_disagreeing_measurement_is_suspect_not_bad(self):
        """A model and a measurement disagreeing does not tell you which is
        wrong. `bad` would NULL a real reading on a model's word."""
        out = {
            v.channel: v
            for v in company.judge(frame(Power=1_080_000.0, DT_model_power=1.2e-4), [self.RULE])
        }
        assert out["Power"].quality != "bad"


class TestItObeysADR132:
    def test_quality_only_ever_uses_the_closed_vocabulary(self):
        rules = [TestTheSignatureItFoundInRealData.RULE, TestPowerWhileEveryRodReadsZero.RULE]
        out = company.judge(
            frame(
                FuelTemp1=0.0,
                FuelTemp2=0.0,
                PoolTemp=23.0,
                Power=1_170_000.0,
                Tran=0.0,
                Shim1=0.0,
                Shim2=0.0,
                Reg=0.0,
            ),
            rules,
        )
        assert out
        assert {v.quality for v in out} <= set(company.QUALITY)

    def test_the_reason_names_the_contradiction_and_not_a_cause(self):
        """Rod-fired pulse, acquisition dropout and a model restart all fit this
        shape. The site owns that call, so the reason states what contradicts
        what — never why."""
        out = company.judge(
            frame(FuelTemp1=0.0, FuelTemp2=0.0, PoolTemp=23.0),
            [TestTheSignatureItFoundInRealData.RULE],
        )
        reasons = " ".join(v.reason for v in out)
        assert "company." in reasons
        for cause in ("dropout", "pulse", "restart", "timeout", "broken", "failure"):
            assert cause not in reasons

    def test_every_verdict_carries_a_sentence_naming_the_other_channel(self):
        """A verdict a human cannot act on is a verdict nobody acts on. The
        whole point is the coincidence, so the message has to name the company."""
        out = company.judge(
            frame(FuelTemp1=0.0, FuelTemp2=0.0, PoolTemp=23.0),
            [TestTheSignatureItFoundInRealData.RULE],
        )
        ft = next(v for v in out if v.channel == "FuelTemp1")
        assert "PoolTemp" in ft.because
        assert "23" in ft.because


class TestAFrameWithNothingWrongIsSilent:
    def test_real_full_power_passes_every_rule(self):
        """The genuine startup from the same series: 930 kW, fuel at 305/366,
        rods at 683-739, model in agreement."""
        rules = [
            TestTheSignatureItFoundInRealData.RULE,
            TestPowerWhileEveryRodReadsZero.RULE,
            TestMeasuredAgainstItsModelTwin.RULE,
        ]
        good = frame(
            FuelTemp1=305.0,
            FuelTemp2=366.0,
            PoolTemp=23.0,
            Power=930_000.0,
            Tran=683.0,
            Shim1=712.0,
            Shim2=739.0,
            Reg=700.0,
            DT_model_power=910_000.0,
        )
        assert company.judge(good, rules) == []

    def test_a_channel_a_rule_never_mentions_is_untouched(self):
        out = company.judge(
            frame(FuelTemp1=0.0, FuelTemp2=0.0, PoolTemp=23.0, WaterTemp=20.0),
            [TestTheSignatureItFoundInRealData.RULE],
        )
        assert "WaterTemp" not in {v.channel for v in out}


class TestTheDeclaredFormat:
    """`rules_from` owns the FORMAT; a consumer owns the file and the judgement.

    The split matters. A consumer's artifact is nested under nouns this layer
    must not know, and the judgement — which channels corroborate which, at what
    threshold — is knowledge about one instrument. But the shape they write it in
    is the platform's to define and to police.
    """

    ZERO = {
        "kind": "zero_while_companion_above",
        "zero": ["FuelTemp1"],
        "companion": ["WaterTemp"],
        "above_degc": 5,
        "reason": "company.zero_while_companion_warm",
    }

    def test_a_declared_rule_becomes_a_working_rule(self):
        rules = company.rules_from([self.ZERO], unit_suffixes=("degc", "w"))
        out = company.judge(frame(FuelTemp1=0.0, WaterTemp=20.0), rules)
        assert {v.channel: v.quality for v in out} == {"FuelTemp1": "bad", "WaterTemp": "suspect"}

    def test_an_unknown_kind_is_refused_not_skipped(self):
        with pytest.raises(ValueError, match="vibes"):
            company.rules_from([{"kind": "vibes", "reason": "company.x"}])

    def test_a_bare_threshold_is_refused_and_the_error_names_the_keys(self):
        bare = {**self.ZERO}
        bare.pop("above_degc")
        bare["above"] = 5
        with pytest.raises(ValueError, match="above_degc"):
            company.rules_from([bare], unit_suffixes=("degc", "w"))

    def test_a_dimensionless_consumer_may_use_the_bare_key(self):
        """`exceeds_twin_by` takes a ratio, which has no unit. Forcing a suffix
        on it would be demanding a unit for a dimensionless quantity."""
        rules = company.rules_from(
            [
                {
                    "kind": "exceeds_twin_by",
                    "subject": "P",
                    "twin": "M",
                    "factor": 100,
                    "reason": "company.measured_exceeds_model_twin",
                }
            ]
        )
        assert company.judge(frame(P=1e6, M=1.0), rules)

    def test_a_reason_naming_a_cause_is_refused(self):
        with pytest.raises(ValueError, match=r"company\."):
            company.rules_from([{**self.ZERO, "reason": "acq.dropout"}], unit_suffixes=("degc",))

    def test_declaring_nothing_yields_nothing(self):
        assert company.rules_from([]) == []
