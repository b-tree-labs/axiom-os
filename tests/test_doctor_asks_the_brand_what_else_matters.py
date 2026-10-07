# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`doctor` ends with a verdict, so it has to have asked the right questions.

Walked from a fresh install on 2026-09-25. `doctor` reported "Environment
looks healthy" to somebody whose whole purpose was to ship data, having
checked Python, the venv, the entry point, the LLM gateway and the working
directory. It knew nothing about whether the install was bound to anything,
whether a credential resolved, or whether an endpoint was reachable — and a
green verdict over questions nobody asked reads as "you are set up".

The platform must not learn what a site is; that is the consumer's word, and
`banner_versions` already says so in as many words: "the platform has no
business knowing what a site is". So the fix is the hook that already exists
for versions, applied to health: the brand contributes its own rows, the
platform renders and counts them, and neither knows the other's vocabulary.
"""

from __future__ import annotations


from axiom.infra.branding import BrandingConfig


def _brand(**kw):
    return BrandingConfig(
        cli_name="demo",
        product_name="Demo",
        package_name="demo-pkg",
        **kw,
    )


class TestTheHookExists:
    def test_a_brand_may_supply_health_checks(self):
        brand = _brand(health_extras_fn=lambda: [])
        assert callable(brand.health_extras_fn)

    def test_a_brand_that_supplies_none_is_normal(self):
        """Most brands have nothing to add, and the platform's own checks are
        a complete answer for them."""
        assert _brand().health_extras_fn is None


class TestTheRowsReachTheVerdict:
    def _checks(self, monkeypatch, rows):
        import axiom.axiom_cli as cli

        monkeypatch.setattr(
            cli, "_brand_health_extras", lambda: list(rows), raising=False)
        return cli._brand_health_extras()

    def test_a_failing_row_is_carried_as_failing(self, monkeypatch):
        rows = self._checks(monkeypatch, [
            {"name": "Site binding", "ok": False, "status": "not bound",
             "fix": "demo site bind"},
        ])
        assert [r["ok"] for r in rows] == [False]

    def test_a_row_carries_its_fix(self, monkeypatch):
        rows = self._checks(monkeypatch, [
            {"name": "Site binding", "ok": False, "status": "not bound",
             "fix": "demo site bind"},
        ])
        assert rows[0]["fix"] == "demo site bind"


class TestTheBrandCannotBreakTheDoctor:
    def test_a_hook_that_raises_is_survived(self):
        """A brand's health check failing must not be why a diagnostic tool
        cannot run. That is the one command somebody types when things are
        already wrong."""
        import axiom.axiom_cli as cli

        def boom():
            raise RuntimeError("brand is broken")

        brand = _brand(health_extras_fn=boom)
        assert cli._brand_health_extras(brand) == []

    def test_a_hook_returning_nonsense_is_survived(self):
        import axiom.axiom_cli as cli

        brand = _brand(health_extras_fn=lambda: "not a list")
        assert cli._brand_health_extras(brand) == []

    def test_rows_missing_required_keys_are_dropped(self):
        """A row with no name renders as a blank line and counts toward a
        verdict nobody can read."""
        import axiom.axiom_cli as cli

        brand = _brand(health_extras_fn=lambda: [
            {"status": "no name"},
            {"name": "Good", "ok": True, "status": "fine"},
        ])
        assert [r["name"] for r in cli._brand_health_extras(brand)] == ["Good"]

    def test_a_row_with_no_ok_is_not_assumed_healthy(self):
        """Defaulting a missing verdict to True is how a check that never ran
        becomes a check that passed."""
        import axiom.axiom_cli as cli

        brand = _brand(health_extras_fn=lambda: [
            {"name": "Unstated", "status": "who knows"},
        ])
        rows = cli._brand_health_extras(brand)
        assert rows == [] or rows[0]["ok"] is False


class TestThePlatformStaysDomainAgnostic:
    def test_the_platform_names_no_consumer_vocabulary(self):
        """The hook is how a consumer contributes without the platform
        learning its nouns."""
        import inspect

        import axiom.axiom_cli as cli

        source = inspect.getsource(cli._brand_health_extras)
        for noun in ("site", "ingest", "reactor", "channel"):
            assert noun not in source.lower(), noun
