# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A site's rules arrive as data. Nothing site-specific is imported."""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.data_platform import site_rules

DECL = {
    "unit_suffixes": ["degc", "w"],
    "rules": [
        {
            "kind": "zero_while_companion_above",
            "zero": ["FuelTemp1", "FuelTemp2"],
            "companion": ["WaterTemp"],
            "above_degc": 5,
            "reason": "company.zero_while_companion_warm",
        }
    ],
}


def _write(state_dir, site, body):
    d = state_dir / site_rules.RULES_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{site}.json").write_text(json.dumps(body))


class TestReadingADeclaration:
    def test_declared_rules_load(self, tmp_path):
        _write(tmp_path, "site-a", DECL)
        assert len(site_rules.rules_for_site("site-a", tmp_path)) == 1

    def test_a_site_with_no_file_has_declared_nothing(self, tmp_path):
        assert site_rules.rules_for_site("nobody", tmp_path) == []

    def test_an_empty_rules_list_is_the_same_as_none(self, tmp_path):
        _write(tmp_path, "quiet", {"rules": []})
        assert site_rules.rules_for_site("quiet", tmp_path) == []

    def test_the_sites_own_unit_suffixes_are_honoured(self, tmp_path):
        """Which units a site's thresholds carry is a fact about its
        instruments, so the site supplies them — and the platform still enforces
        that a threshold key bears one."""
        bare = {**DECL, "rules": [{**DECL["rules"][0]}]}
        bare["rules"][0].pop("above_degc")
        bare["rules"][0]["above"] = 5
        _write(tmp_path, "s", bare)
        with pytest.raises(ValueError, match="above_degc"):
            site_rules.rules_for_site("s", tmp_path)


class TestOneBadDeclarationIsContained:
    def test_malformed_json_raises_for_that_site_only(self, tmp_path):
        d = tmp_path / site_rules.RULES_DIR
        d.mkdir(parents=True)
        (d / "broken.json").write_text("{not json")
        _write(tmp_path, "fine", DECL)
        with pytest.raises(ValueError, match="not valid JSON"):
            site_rules.rules_for_site("broken", tmp_path)
        assert site_rules.rules_for_site("fine", tmp_path)

    def test_a_non_object_declaration_says_what_it_wanted(self, tmp_path):
        d = tmp_path / site_rules.RULES_DIR
        d.mkdir(parents=True)
        (d / "listy.json").write_text("[]")
        with pytest.raises(ValueError, match="'rules' list"):
            site_rules.rules_for_site("listy", tmp_path)

    def test_an_unknown_kind_still_raises_from_the_platform_parser(self, tmp_path):
        _write(tmp_path, "s", {"rules": [{"kind": "vibes", "reason": "company.x"}]})
        with pytest.raises(ValueError, match="vibes"):
            site_rules.rules_for_site("s", tmp_path)


class TestTheLookupReadsEachFileOnce:
    def test_a_second_ask_does_not_re_read(self, tmp_path):
        _write(tmp_path, "ut", DECL)
        it = site_rules.lookup(tmp_path)
        first = it("ut")
        # Removing the file must not change the answer within one pass.
        (tmp_path / site_rules.RULES_DIR / "ut.json").unlink()
        assert it("ut") is first

    def test_each_site_is_read_separately(self, tmp_path):
        _write(tmp_path, "a", DECL)
        it = site_rules.lookup(tmp_path)
        assert it("a")
        assert it("b") == []

    def test_absence_is_cached_too_so_a_missing_file_is_not_probed_per_frame(self, tmp_path):
        it = site_rules.lookup(tmp_path)
        assert it("nobody") == []
        _write(tmp_path, "nobody", DECL)
        assert it("nobody") == []  # still the cached answer for this pass


class TestWhereTheDeclarationLives:
    def test_one_file_per_site_so_installing_one_cannot_touch_another(self, tmp_path):
        a = site_rules.declaration_path("site-a", tmp_path)
        b = site_rules.declaration_path("site-b", tmp_path)
        assert a != b
        assert a.parent == b.parent

    def test_it_sits_under_the_state_dir(self, tmp_path):
        p = site_rules.declaration_path("s", tmp_path)
        assert p.is_relative_to(tmp_path)


class TestNothingIsPaidWhereNothingIsDeclared:
    def test_an_install_with_no_declarations_says_so_in_one_listing(self, tmp_path):
        assert site_rules.any_declared(tmp_path) is False

    def test_one_declaration_is_enough_to_turn_it_on(self, tmp_path):
        _write(tmp_path, "ut", DECL)
        assert site_rules.any_declared(tmp_path) is True

    def test_a_stray_non_json_file_does_not_count(self, tmp_path):
        d = tmp_path / site_rules.RULES_DIR
        d.mkdir(parents=True)
        (d / "README.md").write_text("notes about our rules")
        assert site_rules.any_declared(tmp_path) is False
