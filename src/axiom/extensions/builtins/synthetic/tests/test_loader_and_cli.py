# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Journey files are validated hard; the CLI is a gate."""

from __future__ import annotations

import json
import textwrap

import pytest

from axiom.extensions.builtins.synthetic.cli import get_parser, main
from axiom.extensions.builtins.synthetic.loader import JourneyError, load_journey, parse_journey

from .conftest import SESSION

GOOD = textwrap.dedent("""
    [journey]
    name = "t"
    base_url = "BASE"
    [journey.session]
    cookie_env = "SYNTH_COOKIE"

    [[step]]
    name = "page"
    path = "/"
    auth = "session"
    [step.expect]
    status = 200
    cache_policy = "revalidate"
""")


def test_unknown_keys_are_errors_so_a_typo_cannot_become_a_check_that_never_runs():
    with pytest.raises(JourneyError, match="unknown key"):
        parse_journey(
            {
                "journey": {"name": "n", "base_url": "u"},
                "step": [{"name": "s", "path": "/", "expect": {"statuss": 200}}],
            }
        )


def test_session_step_without_a_cookie_env_is_rejected():
    with pytest.raises(JourneyError, match="cookie_env"):
        parse_journey(
            {
                "journey": {"name": "n", "base_url": "u"},
                "step": [{"name": "s", "path": "/", "auth": "session"}],
            }
        )


def test_unknown_cache_policy_is_rejected():
    with pytest.raises(JourneyError, match="cache_policy"):
        parse_journey(
            {
                "journey": {"name": "n", "base_url": "u"},
                "step": [{"name": "s", "path": "/", "expect": {"cache_policy": "maybe"}}],
            }
        )


def test_missing_file_and_bad_toml_name_the_problem(tmp_path):
    with pytest.raises(JourneyError, match="not found"):
        load_journey(tmp_path / "nope.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[journey\n")
    with pytest.raises(JourneyError, match="not valid TOML"):
        load_journey(bad)


def test_cli_exit_codes_are_a_gate(serve, tmp_path, monkeypatch, capsys):
    f = tmp_path / "j.toml"
    monkeypatch.setenv("SYNTH_COOKIE", SESSION)
    bad = serve(shell_cache_control=None)
    f.write_text(GOOD.replace("BASE", bad))
    assert main(["run", str(f)]) == 1
    assert "heuristic" in capsys.readouterr().out

    ok = serve(shell_cache_control="no-cache")
    f.write_text(GOOD.replace("BASE", ok))
    assert main(["run", str(f), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"

    monkeypatch.delenv("SYNTH_COOKIE")
    assert main(["run", str(f)]) == 3

    assert main(["run", str(tmp_path / "missing.toml")]) == 2


def test_the_program_name_follows_the_active_branding():
    from axiom.infra import branding

    branding.reset()
    assert get_parser().prog == "axi synthetic"
