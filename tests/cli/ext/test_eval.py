# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext eval`` — detect + run eval suites."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from axiom.cli.ext.commands.eval_verb import EvalProvider, _detect_eval_runner
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def run_eval_cli(capsys):
    def _run(path: Path, *extra: str) -> tuple[int, str]:
        capsys.readouterr()
        provider = EvalProvider()
        parser = argparse.ArgumentParser()
        provider.add_arguments(parser)
        args = parser.parse_args([str(path), *extra])
        ctx = CliContext(cwd=path)
        rc = provider.run(args, ctx)
        captured = capsys.readouterr()
        return rc, captured.out

    return _run


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_eval_provider_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "eval" in providers


# ---------------------------------------------------------------------------
# _detect_eval_runner
# ---------------------------------------------------------------------------


def test_detect_returns_none_when_no_suite(scaffolded_extension) -> None:
    ext = scaffolded_extension("no_evals")
    assert _detect_eval_runner(ext) == ("none", None)


def test_detect_promptfoo_yaml(scaffolded_extension) -> None:
    ext = scaffolded_extension("pf_ext")
    evals = ext / "evals"
    evals.mkdir()
    cfg = evals / "promptfooconfig.yaml"
    cfg.write_text("tests: []\n")
    runner, path = _detect_eval_runner(ext)
    assert runner == "promptfoo"
    assert path == cfg


def test_detect_pytest_from_manifest(scaffolded_extension) -> None:
    ext = scaffolded_extension("pytest_ext")
    manifest = ext / "axiom-extension.toml"
    text = manifest.read_text()
    # Append an evals block pointing at pytest.
    text += '\n[extension.evals]\nrunner = "pytest"\n'
    manifest.write_text(text)
    (ext / "evals").mkdir()
    runner, path = _detect_eval_runner(ext)
    assert runner == "pytest"
    assert path == ext / "evals"


# ---------------------------------------------------------------------------
# Provider behavior
# ---------------------------------------------------------------------------


def test_eval_with_no_suite_is_not_a_failure(scaffolded_extension, run_eval_cli) -> None:
    ext = scaffolded_extension("bare_ext")
    rc, out = run_eval_cli(ext)
    # Spec: "not a failure — the CI caller decides policy."
    assert rc == 0
    assert "no eval" in out.lower()


def test_eval_promptfoo_shells_out(scaffolded_extension, run_eval_cli) -> None:
    ext = scaffolded_extension("pf_smoke")
    evals = ext / "evals"
    evals.mkdir()
    (evals / "promptfooconfig.yaml").write_text("tests: []\n")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    with patch("axiom.cli.ext.commands.eval_verb.subprocess.run", return_value=fake_proc) as mock_run:
        rc, out = run_eval_cli(ext)
    assert rc == 0
    # The invoked command must include npx + promptfoo + the yaml path.
    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert "npx" in cmd
    assert "promptfoo" in cmd
    assert str(evals / "promptfooconfig.yaml") in cmd


def test_eval_promptfoo_non_zero_is_surfaced(scaffolded_extension, run_eval_cli) -> None:
    ext = scaffolded_extension("pf_fail")
    evals = ext / "evals"
    evals.mkdir()
    (evals / "promptfooconfig.yaml").write_text("tests: []\n")

    fake_proc = MagicMock()
    fake_proc.returncode = 7
    with patch("axiom.cli.ext.commands.eval_verb.subprocess.run", return_value=fake_proc):
        rc, _ = run_eval_cli(ext)
    assert rc == 7


def test_eval_pytest_runs_evals_dir(scaffolded_extension, run_eval_cli) -> None:
    ext = scaffolded_extension("py_eval")
    manifest = ext / "axiom-extension.toml"
    manifest.write_text(manifest.read_text() + '\n[extension.evals]\nrunner = "pytest"\n')
    (ext / "evals").mkdir()

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    with patch("axiom.cli.ext.commands.eval_verb.subprocess.run", return_value=fake_proc) as mock_run:
        rc, _ = run_eval_cli(ext)
    assert rc == 0
    args, _ = mock_run.call_args
    cmd = args[0]
    assert "pytest" in " ".join(cmd)
    assert str(ext / "evals") in cmd
