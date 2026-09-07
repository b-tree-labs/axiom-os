# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The out-of-the-box changelog: curated when the author wrote one,
synthesized-and-grouped when they didn't, state-file aware for deploys."""

from __future__ import annotations

import json
import logging
import subprocess

import pytest

from axiom.extensions.builtins.release import changelog as cl
from axiom.extensions.builtins.release import skills as release_skills
from axiom.infra.skills import SkillContext, SkillRegistry


def _git(repo, *args, env_extra=None):
    import os

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.org",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.org",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    commits = [
        ("feat: first light", ""),
        ("fix: seal the hatch", ""),
        (
            "feat: hatch dashboard",
            "Operators watch hatch state live instead of walking the floor.\n\nSecond paragraph is ignored.",
        ),
        ("fix: hatch alarm rang twice", ""),
        ("docs: explain the hatch", ""),
    ]
    for i, (subject, body) in enumerate(commits):
        (r / f"f{i}").write_text(str(i))
        _git(r, "add", ".")
        args = ["commit", "-q", "-m", subject] + (["-m", body] if body else [])
        _git(r, *args)
        if i == 1:
            _git(r, "tag", "v1.0.0")
    _git(r, "tag", "v1.1.0")
    return r


def test_build_groups_by_conventional_prefix(repo):
    log = cl.build(repo, from_ref="v1.0.0", to_ref="v1.1.0", changelog_file=None)
    assert log.count == 3
    titles = dict(log.sections())
    # Features carry the commit body's first paragraph — the benefit
    assert titles["Features"] == [
        "hatch dashboard — Operators watch hatch state live instead of walking the floor."
    ]
    # Bug fixes stay direct: the subject IS the fix
    assert titles["Bug fixes"] == ["hatch alarm rang twice"]
    assert titles["Other changes"] == ["explain the hatch"]


def test_unknown_from_ref_degrades_to_recent_history(repo):
    log = cl.build(repo, from_ref="v9.9.9", to_ref="v1.1.0", limit=3, changelog_file=None)
    assert log.count == 3


def test_render_text_markdown_json(repo):
    log = cl.build(repo, from_ref="v1.0.0", to_ref="v1.1.0", changelog_file=None)
    text = cl.render(log, "text")
    assert text.startswith("v1.0.0 \u2192 v1.1.0 (3 changes)")
    assert "\u2022 hatch alarm rang twice" in text
    md = cl.render(log, "markdown")
    assert "### v1.0.0 \u2192 v1.1.0" in md and "- explain the hatch" in md and "**Features**" in md
    data = json.loads(cl.render(log, "json"))
    assert data["count"] == 3 and data["commits"][0]["subject"]


def test_curated_changelog_wins_when_present(repo):
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [1.1.0] \u2014 2026-09-01 \u2014 The hatch holds\n\n### Fixed\n- the hatch\n\n"
        "## [1.0.0] \u2014 2026-08-01\n\n- first light\n"
    )
    log = cl.build(repo, from_ref="v1.0.0", to_ref="v1.1.0")
    assert len(log.curated) == 1
    assert "The hatch holds" in log.curated[0]
    text = cl.render(log, "text")
    assert "the hatch" in text and "hatch dashboard" not in text


def test_empty_range_reads_as_redeploy(repo):
    log = cl.build(repo, from_ref="v1.1.0", to_ref="v1.1.0", changelog_file=None)
    assert "redeploy of v1.1.0" in cl.render(log, "text")


def test_skill_since_state_round_trip(repo, tmp_path, capsys):
    reg = SkillRegistry()
    release_skills.bind(reg)
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))
    state = tmp_path / "last-shipped"
    state.write_text("v1.0.0")
    r = reg.invoke(
        "release.changelog",
        {
            "repo": str(repo),
            "to_ref": "v1.1.0",
            "state_file": str(state),
            "update_state": True,
            "ai": "off",
            "changelog_file": None,
        },
        ctx,
    )
    assert r.ok, r.errors
    assert r.value["from"] == "v1.0.0" and r.value["count"] == 3
    assert state.read_text() == "v1.1.0"
    assert "v1.0.0 \u2192 v1.1.0" in capsys.readouterr().out


def test_cli_parser_wires_the_verb():
    from axiom.extensions.builtins.release.cli import _args_to_params, build_parser

    args = build_parser().parse_args(
        [
            "changelog",
            "--from",
            "v1",
            "--to",
            "v2",
            "--format",
            "json",
            "--changelog-file",
            "none",
            "--limit",
            "5",
        ]
    )
    params = _args_to_params(args)
    assert params["verb"] == "changelog"
    assert params["from_ref"] == "v1" and params["to_ref"] == "v2"
    assert params["fmt"] == "json" and params["limit"] == 5
    assert params["changelog_file"] is None


class _FakeGateway:
    available = True

    def __init__(self, text):
        self._text = text
        self.calls = []

    def complete(self, **kw):
        self.calls.append(kw)

        class R:
            pass

        r = R()
        r.text = self._text
        return r


class TestAiPolish:
    GOOD = (
        '{"features": [{"title": "UT EID sign-in", "description": '
        '"Operators reach chat with their existing UT accounts."}], '
        '"fixes": ["The hatch alarm no longer rings twice."], "other": []}'
    )

    def test_polish_groups_and_rephrases(self, repo):
        log = cl.build(repo, from_ref="v1.0.0", to_ref="v1.1.0", changelog_file=None)
        gw = _FakeGateway(self.GOOD)
        log.polished = cl.polish(log, gateway=gw)
        assert log.polished == [
            (
                "Features",
                ["UT EID sign-in — Operators reach chat with their existing UT accounts."],
            ),
            ("Bug fixes", ["The hatch alarm no longer rings twice."]),
        ]
        # the raw subjects were handed to the model, nothing invented client-side
        # subjects AND body benefit-text reach the model
        assert "hatch alarm rang twice" in gw.calls[0]["prompt"]
        assert "walking the floor" in gw.calls[0]["prompt"]
        text = cl.render(log, "text")
        assert "Features" in text and "existing UT accounts" in text

    def test_malformed_json_falls_back_to_deterministic(self, repo):
        log = cl.build(repo, from_ref="v1.0.0", to_ref="v1.1.0", changelog_file=None)
        assert cl.polish(log, gateway=_FakeGateway("sorry, no json here")) is None
        assert "hatch alarm rang twice" in cl.render(log, "text")

    def test_unavailable_gateway_is_none(self, repo):
        gw = _FakeGateway(self.GOOD)
        gw.available = False
        log = cl.build(repo, from_ref="v1.0.0", to_ref="v1.1.0", changelog_file=None)
        assert cl.polish(log, gateway=gw) is None

    def test_skill_ai_on_errors_without_gateway(self, repo, tmp_path):
        reg = SkillRegistry()
        release_skills.bind(reg)
        ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))
        r = reg.invoke(
            "release.changelog",
            {
                "repo": str(repo),
                "to_ref": "v1.1.0",
                "ai": "on",
                "changelog_file": None,
                "_gateway": _FakeGateway("nope"),
            },
            ctx,
        )
        assert not r.ok and "--ai on" in r.errors[0]


class TestPerUserViewed:
    def test_viewed_round_trip_is_per_principal(self, tmp_path):
        cl.write_viewed(tmp_path, "@alice:ut", "v1.0.0")
        cl.write_viewed(tmp_path, "@bob:ut", "v1.1.0")
        assert cl.read_viewed(tmp_path, "@alice:ut") == "v1.0.0"
        assert cl.read_viewed(tmp_path, "@bob:ut") == "v1.1.0"
        assert cl.read_viewed(tmp_path, "@carol:ut") is None

    def test_skill_since_viewed_and_mark_viewed(self, repo, tmp_path, capsys):
        reg = SkillRegistry()
        release_skills.bind(reg)
        ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))
        common = {
            "repo": str(repo),
            "to_ref": "v1.1.0",
            "ai": "off",
            "changelog_file": None,
            "user": "@alice:ut",
        }
        # first look: nothing recorded yet -> whole recent history, then marked
        r = reg.invoke(
            "release.changelog", {**common, "since_viewed": True, "mark_viewed": True}, ctx
        )
        assert r.ok and r.value["since"] is None
        assert cl.read_viewed(tmp_path, "@alice:ut") == "v1.1.0"
        # second look: nothing new since v1.1.0
        r = reg.invoke("release.changelog", {**common, "since_viewed": True}, ctx)
        assert r.ok and r.value["since"] == "v1.1.0" and r.value["count"] == 0
        assert "redeploy" in r.value["rendered"]

    def test_date_since_is_accepted(self, repo):
        log = cl.build(repo, from_ref="2000-01-01", to_ref="v1.1.0", changelog_file=None)
        assert log.count >= 3  # everything is newer than 2000


class TestLastNReleases:
    def test_releases_scope_uses_tag_history(self, repo, tmp_path):
        reg = SkillRegistry()
        release_skills.bind(reg)
        ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))
        r = reg.invoke(
            "release.changelog",
            {"repo": str(repo), "releases": 1, "ai": "off", "changelog_file": None},
            ctx,
        )
        assert r.ok, r.errors
        # last 1 release = v1.1.0, diffed from its predecessor v1.0.0
        assert r.value["to"] == "v1.1.0" and r.value["since"] == "v1.0.0"
        assert r.value["count"] == 3
        # more releases than exist -> whole history
        r = reg.invoke(
            "release.changelog",
            {"repo": str(repo), "releases": 9, "ai": "off", "changelog_file": None},
            ctx,
        )
        assert r.ok and r.value["since"] is None and r.value["count"] == 5


def test_release_umbrella_commits_carry_their_body(tmp_path):
    r = tmp_path / "rel"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "f").write_text("0")
    _git(r, "add", ".")
    _git(
        r,
        "commit",
        "-q",
        "-m",
        "release: v9 — the big one",
        "-m",
        "Operators get the dashboard; the alarm no longer double-rings.",
    )
    log = cl.build(r, from_ref=None, to_ref="HEAD", changelog_file=None)
    [(title, items)] = log.sections()
    assert title == "Releases"
    assert items == [
        "v9 — the big one — Operators get the dashboard; the alarm no longer double-rings."
    ]
