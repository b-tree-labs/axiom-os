# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A message that names a TOML table must still name it after printing.

`axi ext lint` prints its findings through `rich`, which reads `[...]` as
markup. So every message naming a TOML table lost the table:

    [FAIL] AEOS070: extension 'x' has no  block and no `# mcp: ...` annotation

The gap after "no" is where `[extension.mcp]` was. The one piece of information
the reader needs — which block to add — is the one piece the renderer removes,
and it removes it silently: nothing errors, the line just comes out shorter.
This is the whole vocabulary of a TOML linter, so it affects any finding about
any table, not only this rule.

`[FAIL]` survives because `rich` leaves a tag it cannot parse as a style alone,
and `extension.mcp` parses as one. Which means the defect is invisible in
exactly the messages where a reviewer would look for it.
"""

from __future__ import annotations

import pytest

from axiom.cli.ext import _output


@pytest.fixture(autouse=True)
def _plain(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    _output._reset_console_cache()
    yield
    _output._reset_console_cache()


@pytest.mark.parametrize(
    "named",
    [
        "[extension.mcp]",
        "[extension]",
        "[[extension.mcp.tool]]",
        "[project.scripts]",
        "[tool.pytest.ini_options]",
    ],
)
def test_a_toml_table_named_in_a_finding_survives_printing(named, capsys):
    _output.status("fail", "AEOS070", f"extension 'x' has no {named} block")
    out = capsys.readouterr().out
    assert named in out, (
        f"the renderer ate {named}, which is the one thing the reader needs"
    )


def test_the_status_tag_and_the_content_both_come_through(capsys):
    _output.status("fail", "AEOS070", "no [extension.mcp] block")
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "AEOS070" in out
    assert "[extension.mcp]" in out


def test_colour_still_colours(monkeypatch, capsys):
    """The escape must not cost the styling. A fix that prints the table and
    loses the red mark trades one regression for another.

    The styled branch is reached by naming the decision rather than by setting
    FORCE_COLOR: that variable turns five unrelated tests red locally and CI
    never sees it, so it is not how this suite asks for colour.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(_output, "_color_enabled", lambda _stream: True)
    _output._reset_console_cache()
    _output.status("fail", "AEOS070", "no [extension.mcp] block")
    out = capsys.readouterr().out
    assert "\x1b[" in out, "the styled branch stopped emitting any escape sequence"
    assert "[extension.mcp]" in out


def test_a_message_that_looks_like_a_style_is_still_printed_as_written(capsys):
    """Content comes from manifests and file paths, so it may contain anything.
    `[red]` in a finding is text, not an instruction to the renderer."""
    _output.status("warn", "AEOS073", "a value of [red] was declared")
    out = capsys.readouterr().out
    assert "[red]" in out


def test_the_lint_remediation_line_keeps_its_table_names(capsys):
    """The remediation is printed separately from the finding and had the same
    defect twice over: it also carried a literal backslash-n."""
    from axiom.cli.ext.commands.lint import _mcp_block_findings

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        manifest = Path(d) / "axiom-extension.toml"
        manifest.write_text(
            '[extension]\nname = "probe"\nversion = "0.1.0"\naeos_version = "0.1.0"\n',
            encoding="utf-8",
        )
        findings = _mcp_block_findings(manifest)

    assert findings, "nothing was reported, so nothing is under test"
    aeos070 = [f for f in findings if f.code == "AEOS070"]
    assert aeos070, f"expected an AEOS070, got {[f.code for f in findings]}"
    remedy = aeos070[0].remediation
    assert "\\n" not in remedy, (
        "the remediation carries a literal backslash-n, which is what the reader "
        "sees — it reads as a typo in the tool rather than as a newline"
    )
    assert "[extension.mcp]" in remedy
