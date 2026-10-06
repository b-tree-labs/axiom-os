# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What `ext init` writes must pass the `ext lint` it tells you to run next.

`axi ext init` prints its own next steps, and the first is `axi ext lint`. For
the default compound template that works. For the four ADR-005 lifecycle
templates — `conform`, `alerting`, `analytics`, `ingestion` — it did not, and
not marginally: a scaffolded `alerting` extension failed five rules.

    [FAIL] AEOS010: missing required file: pyproject.toml
    [FAIL] AEOS021: manifest schema violation: 'kind' was unexpected
    [FAIL] AEOS031: missing Python package 'alert_probe'
    [FAIL] AEOS050: missing tests/unit_tests/test_standard.py
    [FAIL] AEOS070: extension has no  block and no annotation

Picking a template and immediately following the printed instruction produced
five failures attributable to nothing the author did. A colleague hit it during
onboarding on 2026-10-01 while trying to write a monitor, which is the one
contribution we had asked for.

The same scaffold's README then told them to run `neut ext new` and
`neut dev up`. Neither command exists — not in that CLI, not in any. A
generated document is followed literally by whoever receives it, so the guard
here is not about these two names: **every command a scaffold writes down must
be a command that exists.**
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from axiom.cli.ext.templates import registry as template_registry

#: Every template the CLI offers under `--template`. Parameterised from the
#: registry rather than listed, so a new template is covered the day it lands
#: instead of the day somebody remembers this file.
ALL_TEMPLATE_IDS = sorted(t.id for t in template_registry())


def _scaffold(template_id: str, dest: Path, name: str) -> Path:
    template = next(t for t in template_registry() if t.id == template_id)
    ext_dir = dest / name
    template.create(
        ext_dir,
        name=name,
        owner="an-institution",
        license="Apache-2.0",
        description=f"{name} — scaffolded in a test",
    )
    return ext_dir


def _lint(ext_dir: Path) -> list:
    from axiom.cli.ext.commands.lint import lint_extension

    return [f for f in lint_extension(ext_dir) if getattr(f, "severity", "error") == "error"]


@pytest.mark.parametrize("template_id", ALL_TEMPLATE_IDS)
def test_every_template_passes_the_lint_its_own_next_step_names(template_id, tmp_path):
    ext_dir = _scaffold(template_id, tmp_path, "probe_ext")
    failures = _lint(ext_dir)
    assert not failures, (
        f"`ext init --template {template_id}` wrote something `ext lint` rejects, "
        f"and `ext lint` is the next step init prints: "
        + "; ".join(f"{getattr(f, 'code', '?')}: {getattr(f, 'message', f)}" for f in failures)
    )


@pytest.mark.parametrize("template_id", ALL_TEMPLATE_IDS)
def test_every_template_writes_the_files_aeos_requires(template_id, tmp_path):
    """Named rather than left to the linter, so a failure says which file."""
    ext_dir = _scaffold(template_id, tmp_path, "probe_ext")
    for required in (
        "axiom-extension.toml",
        "pyproject.toml",
        "README.md",
        "probe_ext/__init__.py",
        "tests/unit_tests/test_standard.py",
    ):
        assert (ext_dir / required).exists(), f"{template_id} did not write {required}"


@pytest.mark.parametrize("template_id", ALL_TEMPLATE_IDS)
def test_a_lifecycle_template_still_delivers_its_own_rule_and_its_can_fire_test(
    template_id, tmp_path
):
    """Conformance must not have cost the thing the template is for.

    The point of these templates is one function to fill in plus a test that
    goes red when you gut it. Making them lint-clean by emitting the compound
    layout and dropping the rule would pass every check above and deliver
    nothing.
    """
    if template_id == "compound":
        pytest.skip("the compound template is the layout itself; it carries no rule")
    ext_dir = _scaffold(template_id, tmp_path, "probe_ext")
    bodies = [p.read_text(encoding="utf-8") for p in ext_dir.rglob("*.py")]
    joined = "\n".join(bodies)
    assert "can_fire" in joined or "can fire" in joined, (
        f"{template_id} lost its prove-it-can-fire test, which is the template's point"
    )


# ---------------------------------------------------------------------------
# A generated document is followed literally.
# ---------------------------------------------------------------------------


def _ext_verbs() -> set[str]:
    from axiom.cli.ext.registry import discover_providers

    return set(discover_providers())


#: `axi ext lint`, `neut ext test` — whatever the brand, the noun and verb are
#: the same two tokens.
_EXT_COMMAND = re.compile(r"`(?:axi|neut|axiom)\s+ext\s+([a-z][a-z_-]*)")

#: Any other `<cli> <noun>` pair a scaffold names, so an invented noun like
#: `dev` is caught and not only the two we know about.
_ANY_COMMAND = re.compile(r"`(?:axi|neut|axiom)\s+([a-z][a-z_-]*)")


@pytest.mark.parametrize("template_id", ALL_TEMPLATE_IDS)
def test_no_scaffolded_text_names_an_ext_verb_that_does_not_exist(template_id, tmp_path):
    ext_dir = _scaffold(template_id, tmp_path, "probe_ext")
    real = _ext_verbs()
    for doc in list(ext_dir.rglob("*.md")) + list(ext_dir.rglob("*.py")):
        text = doc.read_text(encoding="utf-8")
        for verb in set(_EXT_COMMAND.findall(text)):
            assert verb in real, (
                f"{doc.relative_to(ext_dir)} tells the reader to run `ext {verb}`, "
                f"which is not a verb. Real ones: {', '.join(sorted(real))}"
            )


@pytest.mark.parametrize("template_id", ALL_TEMPLATE_IDS)
def test_no_scaffolded_text_names_a_noun_the_cli_does_not_have(template_id, tmp_path):
    """`neut dev up` was written into every lifecycle README and there has
    never been a `dev` noun in any of these CLIs."""
    ext_dir = _scaffold(template_id, tmp_path, "probe_ext")
    invented = {"dev"}
    for doc in list(ext_dir.rglob("*.md")) + list(ext_dir.rglob("*.py")):
        text = doc.read_text(encoding="utf-8")
        named = set(_ANY_COMMAND.findall(text))
        assert not (named & invented), (
            f"{doc.relative_to(ext_dir)} names `{', '.join(sorted(named & invented))}`, "
            f"which is not a command in any of these CLIs"
        )


def test_the_pattern_this_guard_relies_on_can_actually_match():
    """A negative control. A regex that matches nothing makes every assertion
    above pass for the wrong reason, and a guard that cannot fail is worse
    than no guard."""
    assert _EXT_COMMAND.findall("run `axi ext lint` first") == ["lint"]
    assert _ANY_COMMAND.findall("try `neut dev up` now") == ["dev"]
    assert "lint" in _ext_verbs(), "the verb registry came back without `lint`"


# ---------------------------------------------------------------------------
# Lint is not the test. `ext init` prints `ext test` as its second step, and a
# scaffold whose own tests do not run is the same failure one step later.
# ---------------------------------------------------------------------------

LIFECYCLE_IDS = [t for t in ALL_TEMPLATE_IDS if t != "compound"]


def _pytest_in(ext_dir: Path, target: str) -> tuple[int, str]:
    import subprocess
    import sys

    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
        cwd=ext_dir,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.mark.parametrize("template_id", LIFECYCLE_IDS)
def test_the_rule_test_a_template_writes_actually_runs(template_id, tmp_path):
    """The import path changed when the rule moved into the package, and an
    import error here reads to a newcomer as "the scaffold is broken"."""
    ext_dir = _scaffold(template_id, tmp_path, "probe_ext")
    target = f"tests/unit_tests/test_{template_id}_rule.py"
    code, output = _pytest_in(ext_dir, target)
    assert code == 0, f"{template_id}'s own rule test does not pass as written:\n{output}"


@pytest.mark.parametrize("template_id", LIFECYCLE_IDS)
def test_gutting_the_rule_turns_the_can_fire_guard_red(template_id, tmp_path):
    """The property the templates exist to teach, proven rather than asserted.

    Every one of these ships a test whose comment says it goes red when you gut
    the function. A guard that cannot fail is worse than no guard, so this guts
    the function and checks that it does.
    """
    ext_dir = _scaffold(template_id, tmp_path, "probe_ext")
    main = next(
        p
        for p in (ext_dir / "probe_ext").glob("*.py")
        if p.name not in ("__init__.py",)
    )
    body = main.read_text(encoding="utf-8")
    # Gut every function: keep the signature, return nothing useful.
    gutted = re.sub(
        r"(\ndef [a-zA-Z_][\w]*\([^)]*\)[^:]*:\n)(?:(?:    .*|\s*)\n)+",
        r"\1    return []\n",
        body,
    )
    assert gutted != body, "the gutting pattern matched nothing, so this proves nothing"
    main.write_text(gutted, encoding="utf-8")

    code, output = _pytest_in(ext_dir, f"tests/unit_tests/test_{template_id}_rule.py")
    assert code != 0, (
        f"{template_id}'s rule was gutted and its tests still passed — "
        f"the prove-it-can-fire guard cannot fire:\n{output}"
    )


# ---------------------------------------------------------------------------
# `ext test` is often the second command somebody runs on a fresh machine, so
# its first failure is their first impression of whether any of this works.
# ---------------------------------------------------------------------------


def test_ext_test_says_what_is_missing_rather_than_handing_over_pytests_words():
    from axiom.cli.ext.commands.test_verb import missing_deps_message

    said = missing_deps_message(["pytest"])
    assert "pip install pytest" in said
    assert "ext test" in said, "the message must name the command the reader typed"


def test_the_message_names_the_plugin_too_because_its_absence_reads_worse():
    """A missing `axiom_tests` fails the standard conformance test on an
    unknown fixture rather than on a missing import, which is the harder of the
    two to read."""
    from axiom.cli.ext.commands.test_verb import missing_deps_message

    said = missing_deps_message(["pytest", "axiom_tests"])
    assert "pytest" in said and "axiom-tests" in said
    assert "are not installed" in said, "two missing packages read as one"


def test_nothing_is_reported_missing_when_everything_is_installed():
    """A negative control: this suite runs under pytest with the plugin loaded,
    so an empty answer here is the only correct one. A check that always finds
    something missing would block the verb for everybody."""
    from axiom.cli.ext.commands.test_verb import missing_test_deps

    assert missing_test_deps() == []


def test_the_verb_refuses_rather_than_shelling_out_when_a_dep_is_absent(tmp_path, monkeypatch):
    from axiom.cli.ext.commands import test_verb

    monkeypatch.setattr(test_verb, "missing_test_deps", lambda: ["pytest"])
    called = {"ran": False}

    def _never(*_a, **_k):
        called["ran"] = True
        raise AssertionError("shelled out to a pytest that is not installed")

    monkeypatch.setattr(test_verb.subprocess, "run", _never)
    (tmp_path / "tests").mkdir()
    assert test_verb.run_pytest(tmp_path) == 1
    assert called["ran"] is False


# ---------------------------------------------------------------------------
# One scaffold names one CLI. A consumer distribution renames it, so a template
# that hardcodes `axi` hands a consumer-layer user instructions for a command that is
# not theirs — and a page naming both reads as two products.
# ---------------------------------------------------------------------------

_CLI_NAMES = ("axi", "neut", "axiom")


def _scaffolded_text(ext_dir: Path) -> list[Path]:
    """Every generated file a reader reads instructions out of.

    The TOML files are in scope because the generated manifest and pyproject
    carry commands in their comments, and a comment telling somebody to run a
    command they do not have is the same defect as a README doing it.
    """
    out: list[Path] = []
    for pattern in ("*.md", "*.py", "*.toml"):
        out.extend(ext_dir.rglob(pattern))
    return out


def _cli_names_in(text: str) -> set[str]:
    """Which CLI names a piece of text tells the reader to run."""
    found = set()
    for name in _CLI_NAMES:
        if re.search(rf"`{name}\s+[a-z]", text) or re.search(rf"^\s*{name}\s+[a-z]", text, re.M):
            found.add(name)
    return found


@pytest.mark.parametrize("template_id", ALL_TEMPLATE_IDS)
def test_a_scaffold_names_one_cli_and_it_is_the_branded_one(template_id, tmp_path, monkeypatch):
    """Renaming the CLI must rename it everywhere the scaffold writes it.

    Driven by actually renaming it, because asserting the default name would pass
    against text that hardcodes the default.

    The rename goes in at the single source — the branding registry — rather than
    at each template's helper. Patching the helpers would pass against a template
    that reads the brand through some third route, and there are two helpers
    already.
    """
    from dataclasses import replace

    from axiom.infra import branding

    # The original, captured before patching. Calling the patched accessor from
    # inside its own replacement recurses, and every caller swallows the error
    # into the default name — which looks exactly like a template that ignored
    # the brand, and cost a debugging round here.
    renamed = replace(branding.get_branding(), cli_name="zzcli")
    monkeypatch.setattr(branding, "get_branding", lambda: renamed)
    ext_dir = _scaffold(template_id, tmp_path, "probe_ext")

    for doc in _scaffolded_text(ext_dir):
        named = _cli_names_in(doc.read_text(encoding="utf-8"))
        assert not named, (
            f"{doc.relative_to(ext_dir)} tells the reader to run "
            f"{', '.join(sorted(named))} after the CLI was renamed — a consumer "
            f"distribution's user is told to run a command they do not have"
        )


@pytest.mark.parametrize("template_id", ALL_TEMPLATE_IDS)
def test_no_scaffolded_page_names_two_different_clis(template_id, tmp_path):
    """The reader's side of the same defect. A page that opens with one command
    and continues with another reads as two products."""
    ext_dir = _scaffold(template_id, tmp_path, "probe_ext")
    for doc in _scaffolded_text(ext_dir):
        named = _cli_names_in(doc.read_text(encoding="utf-8"))
        assert len(named) <= 1, (
            f"{doc.relative_to(ext_dir)} names more than one CLI "
            f"({', '.join(sorted(named))})"
        )


def test_the_cli_detector_detects(tmp_path):
    """A negative control: a matcher that matches nothing makes both tests above
    pass for the wrong reason."""
    assert _cli_names_in("run `axi ext test` now") == {"axi"}
    assert _cli_names_in("```\nneut ext test\n```") == {"neut"}
    assert _cli_names_in("`axi ext test` then `neut ext test`") == {"axi", "neut"}
    # Prose mentioning the product, not a command, is not a CLI instruction.
    assert _cli_names_in("the axiom platform") == set()
