# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-S2 and LOOP-S3: the push is the gate, and the content guards run before the
push and again on the server (ADR-257 D3, D7; PRD the loop section 5.2).

The cheap guards are defined once (``release/guards.py``) and run by the pre-push hook
and by ``loop-guards.yml`` on every push to main, so a push that skipped the hook is
still checked. The tier map (``.axiom/tiers.toml``, ADR-257 D2) is one declared file;
the hook, the server check and the lander compute and report a change's tier from it.
Nothing is refused for the tier in this phase.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from axiom.extensions.builtins.hygiene._git_isolation import git_isolated_env
from axiom.extensions.builtins.release import guards, tiers
from axiom.extensions.builtins.release.lander.config import LanderConfig
from axiom.extensions.builtins.release.lander.model import pull_from_mapping
from axiom.extensions.builtins.release.lander.render import render_text
from axiom.extensions.builtins.release.lander.report import build_report

REPO_ROOT = Path(__file__).resolve().parents[6]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PRE_PUSH = REPO_ROOT / "scripts" / "hooks" / "pre-push"
GUARDS_COMMAND = "-m axiom.extensions.builtins.release.cli guards"

#: Built at run time, so this file never holds a credential-shaped string.
FAKE_TOKEN = "gh" + "p_" + "A1b2C3d4" * 5


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=root, env=git_isolated_env(), capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _commit(root: Path, path: str, text: str, message: str = "change") -> str:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(root, "add", path)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _ok_runner(args, **_kwargs):
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _on(workflow: dict) -> dict:
    # PyYAML reads the bare key `on` as True.
    return workflow.get("on") or workflow.get(True) or {}


def _runs(workflow: dict) -> str:
    return "\n".join(
        str(step.get("run") or "")
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
    )


# -- the guards are defined once -----------------------------------------------


@pytest.mark.acceptance("LOOP-S3")
def test_the_cheap_guards_are_defined_once():
    ids = [guard.id for guard in guards.GUARDS]
    assert ids == ["root-dirs", "public-surface", "naming", "adr-numbers", "secrets"]
    for guard in guards.GUARDS:
        for target in guard.targets:
            assert (REPO_ROOT / target.split("::", 1)[0]).is_file(), target


@pytest.mark.acceptance("LOOP-S3")
def test_a_pushed_credential_is_refused_and_never_echoed(tmp_path: Path):
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "src/settings.py", f"TOKEN = '{FAKE_TOKEN}'\n")
    report = guards.run_guards(root, base=base, head=head, only=("secrets",), runner=_ok_runner)
    assert not report.ok
    result = report.result("secrets")
    assert not result.ok
    assert "src/settings.py:1" in result.detail
    assert "github-token" in result.detail
    assert FAKE_TOKEN not in report.render()
    assert FAKE_TOKEN not in str(report.to_dict())


@pytest.mark.acceptance("LOOP-S3")
def test_only_the_pushed_lines_are_scanned(tmp_path: Path):
    root = _repo(tmp_path)
    _commit(root, "tests/fixture.py", f"TOKEN = '{FAKE_TOKEN}'\n")
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "docs/note.md", "a note\n")
    report = guards.run_guards(root, base=base, head=head, only=("secrets",), runner=_ok_runner)
    assert report.ok, report.render()


@pytest.mark.acceptance("LOOP-S3")
def test_a_fixture_line_marked_allowed_passes(tmp_path: Path):
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "tests/fixture.py", f"TOKEN = '{FAKE_TOKEN}'  # secret-scan: allow\n")
    report = guards.run_guards(root, base=base, head=head, only=("secrets",), runner=_ok_runner)
    assert report.ok, report.render()


@pytest.mark.acceptance("LOOP-S3")
def test_a_failing_guard_is_named_and_the_others_pass(tmp_path: Path):
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "docs/note.md", "a note\n")

    def runner(args, **_kwargs):
        if "pytest" in args:
            out = "FAILED tests/test_mirror.py::test_public_surface_has_no_forbidden_terms - AssertionError\n"
            return subprocess.CompletedProcess(args, 1, stdout=out, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    report = guards.run_guards(root, base=base, head=head, runner=runner)
    assert not report.ok
    assert [r.id for r in report.results if not r.ok] == ["public-surface"]
    assert "✗ public-surface" in report.render()


@pytest.mark.acceptance("LOOP-S3")
def test_a_guard_that_cannot_run_blocks_rather_than_passing(tmp_path: Path):
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "docs/note.md", "a note\n")

    def runner(args, **_kwargs):
        if any(str(a).endswith("lint_adr_numbers.py") for a in args):
            return subprocess.CompletedProcess(args, 127, stdout="", stderr="not found")
        if "pytest" in args:
            return subprocess.CompletedProcess(args, 4, stdout="", stderr="usage error")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    report = guards.run_guards(root, base=base, head=head, runner=runner)
    failed = {r.id: r.detail for r in report.results if not r.ok}
    assert set(failed) == {"root-dirs", "public-surface", "naming", "adr-numbers"}
    assert "could not run" in failed["adr-numbers"]
    assert "could not run" in failed["naming"]


@pytest.mark.acceptance("LOOP-S3")
def test_the_pytest_guards_run_without_the_suite_s_conftest_or_plugins(tmp_path: Path):
    """The hook and a bare server runner must run the same command, so it needs
    neither the platform's conftest nor an installed plugin."""
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "docs/note.md", "a note\n")
    seen: list[tuple[list[str], dict]] = []

    def runner(args, **kwargs):
        seen.append((list(args), dict(kwargs.get("env") or {})))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    guards.run_guards(root, base=base, head=head, runner=runner, python="/usr/bin/python3")
    pytest_calls = [(a, e) for a, e in seen if "pytest" in a]
    assert len(pytest_calls) == 1
    args, env = pytest_calls[0]
    assert args[:3] == ["/usr/bin/python3", "-m", "pytest"]
    assert "--noconftest" in args
    assert env.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"


# -- the hook and the server run the one definition ------------------------------


@pytest.mark.acceptance("LOOP-S2", "LOOP-S3")
def test_the_hook_runs_the_one_definition_before_the_suite():
    text = PRE_PUSH.read_text(encoding="utf-8")
    assert GUARDS_COMMAND in text
    # The node ids that used to be listed in the hook now live in guards.py only.
    assert "test_no_unexpected_root_dirs" not in text
    assert "test_public_surface_has_no_forbidden_terms" not in text
    assert text.index(GUARDS_COMMAND) < text.index("throwaway_postgres.py create")


@pytest.mark.acceptance("LOOP-S3")
def test_the_server_runs_the_same_guards_on_every_push_to_main():
    workflow = _workflow("loop-guards.yml")
    on = _on(workflow)
    assert on["push"]["branches"] == ["main"]
    assert "paths" not in on["push"] and "paths-ignore" not in on["push"]
    assert "pull_request" in on and "merge_group" in on
    assert GUARDS_COMMAND in _runs(workflow)
    job = workflow["jobs"]["guards"]
    assert job["name"] == "Guards"
    # Every main commit gets its own verdict: a newer push never cancels or replaces it.
    group = workflow["concurrency"]["group"]
    assert "github.sha" in group
    assert workflow["permissions"] == {"contents": "read"}


@pytest.mark.acceptance("LOOP-S3")
def test_the_server_judges_the_pushed_range_not_only_the_tip():
    text = (WORKFLOWS / "loop-guards.yml").read_text(encoding="utf-8")
    assert "github.event.before" in text
    assert "github.event.pull_request.base.sha" in text
    assert "fetch-depth: 0" in text


# -- the tier map -------------------------------------------------------------------


_MAP = {
    "version": 1,
    "default": 1,
    "rule": [
        {"tier": 0, "name": "documents", "paths": ["docs/**", "**/*.md"]},
        {"tier": 0, "name": "tests", "paths": ["tests/**", "**/tests/**"]},
        {"tier": 2, "name": "CI workflows and hooks", "paths": [".github/**", "scripts/hooks/**"]},
        {"tier": 2, "name": "agent instructions", "paths": ["AGENTS.md"]},
    ],
}
_CODEOWNERS = """
# comment
/src/app/auth/        @owner
/src/app/authority.py @owner
pyproject.toml        @owner
"""


@pytest.mark.acceptance("LOOP-S2")
def test_a_path_takes_the_highest_tier_of_every_rule_it_matches():
    tier_map = tiers.parse_tier_map(_MAP, codeowners_text=_CODEOWNERS)
    assert tier_map.tier_of("docs/guide.md").tier == 0
    assert tier_map.tier_of("tests/test_x.py").tier == 0
    assert tier_map.tier_of("src/app/feature.py").tier == 1
    assert tier_map.tier_of(".github/workflows/ci.yml").tier == 2
    assert tier_map.tier_of("AGENTS.md").tier == 2
    assert tier_map.tier_of("src/app/auth/login.py").tier == 2
    assert tier_map.tier_of("src/app/authority.py").tier == 2
    assert tier_map.tier_of("src/app/authority_rules.py").tier == 1
    assert tier_map.tier_of("packages/x/pyproject.toml").tier == 2
    reading = tier_map.tier_of("src/app/auth/tests/test_login.py")
    assert reading.tier == 2 and "CODEOWNERS" in " ".join(reading.reasons)


@pytest.mark.acceptance("LOOP-S2")
def test_a_change_is_the_highest_tier_of_any_path_it_touches():
    tier_map = tiers.parse_tier_map(_MAP, codeowners_text=_CODEOWNERS)
    assert tier_map.classify(["docs/a.md", "tests/test_a.py"]).tier == 0
    assert tier_map.classify(["docs/a.md", "src/app/feature.py"]).tier == 1
    change = tier_map.classify(["docs/a.md", "src/app/feature.py", "scripts/hooks/pre-push"])
    assert change.tier == 2
    assert change.deciding == ("scripts/hooks/pre-push",)
    assert "tier 2" in change.line()
    assert tier_map.classify([]).tier == 0


@pytest.mark.acceptance("LOOP-S2")
def test_a_bad_tier_map_is_refused_not_guessed():
    with pytest.raises(ValueError):
        tiers.parse_tier_map({"default": 1, "rule": [{"tier": 3, "name": "x", "paths": ["a"]}]})
    with pytest.raises(ValueError):
        tiers.parse_tier_map({"default": 1, "rule": [{"tier": 0, "name": "x", "paths": []}]})


@pytest.mark.acceptance("LOOP-S2")
def test_this_repository_declares_its_tier_map_seeded_from_codeowners():
    tier_map = tiers.load_tier_map(REPO_ROOT)
    assert tier_map is not None
    assert tier_map.source.endswith(".axiom/tiers.toml")
    assert tier_map.tier_of("docs/guides/your-first-week.md").tier == 0
    assert tier_map.tier_of("tests/test_cli_table.py").tier == 0
    assert tier_map.tier_of("src/axiom/extensions/builtins/release/lander/render.py").tier == 1
    for path in (
        ".github/workflows/ci.yml",
        "scripts/hooks/pre-push",
        "pyproject.toml",
        "AGENTS.md",
        ".axiom/tiers.toml",
        "src/axiom/extensions/builtins/release/guards.py",
        "tests/test_axiom_names_no_domain.py",
        "src/axiom/extensions/builtins/features/resolver.py",
    ):
        assert tier_map.tier_of(path).tier == 2, path
    codeowners = (REPO_ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    for line in codeowners.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pattern = line.split()[0].lstrip("/")
        sample = pattern + "x.py" if pattern.endswith("/") else pattern
        assert tier_map.tier_of(sample).tier == 2, sample


@pytest.mark.acceptance("LOOP-S2")
def test_the_guards_report_the_tier_and_refuse_nothing_for_it(tmp_path: Path):
    root = _repo(tmp_path)
    (root / ".axiom").mkdir()
    (root / ".axiom" / "tiers.toml").write_text(
        'version = 1\ndefault = 1\n[[rule]]\ntier = 2\nname = "CI workflows and hooks"\npaths = [".github/**"]\n'
    )
    _git(root, "add", ".axiom/tiers.toml")
    _git(root, "commit", "-q", "-m", "map")
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, ".github/workflows/x.yml", "name: x\n")
    report = guards.run_guards(root, base=base, head=head, runner=_ok_runner)
    assert report.ok, report.render()
    assert report.tier is not None and report.tier.tier == 2
    assert "tier 2" in report.render()
    assert report.to_dict()["tier"]["tier"] == 2


@pytest.mark.acceptance("LOOP-S2")
def test_no_tier_map_means_no_tier_and_nothing_refused(tmp_path: Path):
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, ".github/workflows/x.yml", "name: x\n")
    report = guards.run_guards(root, base=base, head=head, runner=_ok_runner)
    assert report.ok
    assert report.tier is None
    assert "no tier map" in report.render()


@pytest.mark.acceptance("LOOP-S2")
def test_the_lander_reports_each_pull_request_s_tier():
    tier_map = tiers.parse_tier_map(_MAP)
    pulls = [
        pull_from_mapping({"number": 1, "title": "docs", "head_sha": "a", "head_ref": "d", "files": ["docs/a.md"], "checks": "passing"}),
        pull_from_mapping({"number": 2, "title": "ci", "head_sha": "b", "head_ref": "c", "files": [".github/workflows/x.yml"], "checks": "passing"}),
    ]
    report = build_report(pulls, repo="example-org/app", config=LanderConfig(), tier_map=tier_map)
    views = {v.number: v for v in [*report.ready, *report.needs_human, *report.waiting_on_ci]}
    assert views[1].tier == 0
    assert views[2].tier == 2
    text = render_text(report)
    assert "#1" in text and "tier=0" in text
    assert "tier=2" in text
    assert report.to_dict()["ready"][0]["tier"] == 0
