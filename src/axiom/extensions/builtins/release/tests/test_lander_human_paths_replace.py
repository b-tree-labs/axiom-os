# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``human_paths_mode = "replace"``: the repo's table is the whole protected set."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.fixes import Diagnosis, MechanicalFixProvider
from axiom.extensions.builtins.release.handoff import WebhookHandoffFixProvider
from axiom.extensions.builtins.release.lander.actor import fixer_block, posture
from axiom.extensions.builtins.release.lander.config import LanderConfig, parse_config
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.protect import config_categories, protected_categories
from axiom.extensions.builtins.release.lander.report import build_report
from axiom.policy.agent_action_guard import human_route

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

PRICING_ONLY = """
human_paths_mode = "replace"

[human_paths]
pricing = ['(^|/)(pricing|billing)(/|$)']
"""

#: The replace-mode table one downstream repo ships, verbatim.
DOWNSTREAM = r"""
human_paths_mode = "replace"

[human_paths]
pricing = [
  '^(?!docs/)(.*/)?(pricing|billing|stripe)(/|$)',
  '^(?!docs/)(.*/)?[^/]*(pricing|billing|stripe)[^/]*\.(py|jsx?|tsx?)$',
  '(^|/)tier_service\.py$',
]
security = [
  '^(?!docs/)(.*/)?(security|auth|secrets?)(/|$)',
  '^(?!docs/)(.*/)?(auth|secrets?|credentials?)(\.[^/]+)?$',
]
data = [
  '(^|/)migrations?(/|$)',
  '(^|/)schema(/|$)',
  '\.sql$',
]
deploy = [
  '(^|/)\.github/workflows/[^/]*(deploy|release|promote|terraform)[^/]*$',
  '^infrastructure/terraform/environments/(prod|stg)/',
]
"""


def _pull(**kwargs) -> OpenPull:
    base = dict(
        number=1,
        title="change",
        head_sha="abc",
        head_ref="topic/one",
        updated_at=T0,
        created_at=T0,
        checks="passing",
        mergeable=True,
        files=("src/a.py",),
        draft=False,
    )
    base.update(kwargs)
    return OpenPull(**base)


def test_human_route_replace_uses_only_the_given_categories():
    extra = {"pricing": ("(^|/)(pricing|billing)(/|$)",)}
    assert human_route(["tests/conftest.py"], extra=extra, replace=True) == ()
    assert human_route(["src/auth/session.py"], extra=extra, replace=True) == ()
    assert human_route(["src/billing/x.py"], extra=extra, replace=True) == ("pricing",)
    # Default behaviour is unchanged.
    assert human_route(["tests/conftest.py"], extra=extra) == ("test-setup",)
    assert human_route(["src/auth/session.py"]) == ("security",)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".github/workflows/ci.yml", ()),
        ("requirements.txt", ()),
        ("conftest.py", ()),
        ("docs/x.md", ()),
        ("src/auth/session.py", ()),
        ("src/billing/x.py", ("pricing",)),
    ],
)
def test_replace_mode_narrows_the_protected_set(path, expected):
    config = parse_config(PRICING_ONLY)
    assert config.replaces_human_paths
    assert config_categories((path,), config) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".github/workflows/ci.yml", ("test-setup", "workflow")),
        ("requirements.txt", ("dependency",)),
        ("conftest.py", ("test-setup",)),
        ("docs/x.md", ()),
        ("src/billing/x.py", ("pricing",)),
    ],
)
def test_extend_mode_keeps_the_built_in_additions(path, expected):
    for config in (parse_config(PRICING_ONLY.replace('"replace"', '"extend"')), LanderConfig()):
        assert not config.replaces_human_paths
        assert config_categories((path,), config) == expected


def test_protected_categories_extend_is_the_default():
    extend = protected_categories(
        (".github/workflows/ci.yml",),
        dependency_patterns=("(^|/)requirements\\.txt$",),
        test_setup_patterns=("(^|/)conftest\\.py$",),
    )
    assert extend == ("test-setup", "workflow")
    replaced = protected_categories(
        (".github/workflows/ci.yml", "requirements.txt", "conftest.py"),
        extra={"pricing": ("billing",)},
        dependency_patterns=("(^|/)requirements\\.txt$",),
        test_setup_patterns=("(^|/)conftest\\.py$",),
        replace=True,
    )
    assert replaced == ()


@pytest.mark.parametrize(
    "text",
    [
        'human_paths_mode = "replace"\n',
        'human_paths_mode = "replace"\n[human_paths]\n',
        'human_paths_mode = "replace"\n[human_paths]\npricing = []\n',
    ],
)
def test_an_empty_replacement_is_refused(text):
    with pytest.raises(LanderRefused, match="protect nothing"):
        parse_config(text)


def test_a_replacement_pattern_that_does_not_compile_is_refused():
    with pytest.raises(LanderRefused, match="does not compile"):
        parse_config('human_paths_mode = "replace"\n[human_paths]\npricing = ["(unclosed"]\n')


def test_an_empty_table_in_extend_mode_is_still_accepted():
    assert parse_config("[human_paths]\n").extra_path_rules == {}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("services/webapp/api/routes/billing.py", ("pricing",)),
        ("docs/product/pricing-tiers.md", ()),
        ("services/webapp/deployment/migrations/x.py", ("data",)),
        (".github/workflows/15-deploy-app-stg.yml", ("deploy",)),
        (".github/workflows/16-lint-precommit.yml", ()),
        ("services/webapp/requirements.txt", ()),
        ("services/webapp/Dockerfile.base", ()),
    ],
)
def test_a_downstream_replacement_routes_its_paths(path, expected):
    config = parse_config(DOWNSTREAM)
    assert config_categories((path,), config) == expected
    assert human_route((path,), extra=config.extra_path_rules, replace=True) == expected


def test_the_report_names_the_replacement_categories():
    config = parse_config(DOWNSTREAM)
    pulls = [
        _pull(number=1, head_ref="a/one", files=("services/webapp/api/routes/billing.py",)),
        _pull(number=2, head_ref="b/two", files=(".github/workflows/15-deploy-app-stg.yml",)),
        _pull(number=3, head_ref="c/three", files=("services/webapp/requirements.txt",)),
        _pull(number=4, head_ref="d/four", files=(".github/workflows/16-lint-precommit.yml",)),
    ]
    report = build_report(pulls, repo="acme/widgets", config=config, samples=[15])
    human = {item.number: item.reasons for item in report.needs_human}
    assert "pricing" in human[1]
    assert "deploy" in human[2]
    for number in (3, 4):
        assert not {"dependency", "workflow", "test-setup"} & set(human.get(number, ()))


def test_the_actor_holds_and_releases_by_the_replacement(tmp_path: Path):
    config = parse_config(DOWNSTREAM)
    assert fixer_block(_pull(files=(".github/workflows/15-deploy-app-stg.yml",)), config) == "protected:deploy"
    assert fixer_block(_pull(files=("services/webapp/Dockerfile.base",)), config) == ""
    assert posture("merge", ("services/webapp/api/routes/billing.py",), config, ledger=None) == "approve"


def test_the_mechanical_provider_follows_the_bound_mode():
    pull = _pull(files=("src/auth/session.py",), checks="failing", failure_log="ruff found errors")
    diagnosis = Diagnosis("lint", "ruff found errors", pull.files)
    unbound = MechanicalFixProvider()
    assert unbound.draft(diagnosis, pull).reason == "human_route"
    bound = MechanicalFixProvider()
    bound.bind(config=parse_config(PRICING_ONLY), state_dir=None, repo="", environ={})
    assert bound.draft(diagnosis, pull).reason != "human_route"
    handoff = WebhookHandoffFixProvider()
    handoff.bind(config=parse_config(PRICING_ONLY), state_dir=None, repo="", environ={})
    assert handoff.inner.replace_rules == {"pricing": ("(^|/)(pricing|billing)(/|$)",)}
