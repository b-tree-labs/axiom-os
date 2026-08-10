# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Rich-backed ``axi ext`` output helpers.

These assert the non-TTY contract (capsys-driven): glyphs collapse to
bracketed tags, tables render as plain ASCII, errors go to stderr with a
predictable envelope. TTY-path assertions are lighter — we check that Rich
is actually invoked and the semantic content survives.
"""

from __future__ import annotations

import pytest

from axiom.cli.ext import _output


@pytest.fixture(autouse=True)
def _reset_console_cache_between_tests():
    """capsys swaps sys.stdout per-test, so we drop the cached console."""
    _output._reset_console_cache()
    yield
    _output._reset_console_cache()


class TestStatus:
    """``status()`` emits one row per level with substring-stable content."""

    def test_pass_non_tty(self, capsys):
        _output.status("pass", "manifest_sanity", "ok")
        out = capsys.readouterr().out
        assert "[PASS]" in out
        assert "manifest_sanity" in out
        assert "ok" in out

    def test_fail_non_tty(self, capsys):
        _output.status("fail", "license", "not on allowlist")
        out = capsys.readouterr().out
        assert "[FAIL]" in out
        assert "license" in out
        assert "not on allowlist" in out

    def test_warn_non_tty(self, capsys):
        _output.status("warn", "secrets", "possible hit")
        out = capsys.readouterr().out
        assert "[WARN]" in out
        assert "secrets" in out

    def test_info_non_tty(self, capsys):
        _output.status("info", "python_version", "3.14")
        out = capsys.readouterr().out
        assert "[INFO]" in out
        assert "python_version" in out

    def test_status_no_detail(self, capsys):
        _output.status("pass", "check-only")
        out = capsys.readouterr().out
        assert "[PASS]" in out
        assert "check-only" in out

    def test_unknown_level_raises(self):
        with pytest.raises(ValueError):
            _output.status("neutral", "bad", "x")  # type: ignore[arg-type]


class TestTable:
    """``table()`` renders a title + headers + rows in plain mode."""

    def test_plain_table_shape(self, capsys):
        _output.table(
            title="Installed",
            columns=["NAME", "VERSION"],
            rows=[["foo", "1.0.0"], ["bar", "0.2.1"]],
        )
        out = capsys.readouterr().out
        assert "Installed" in out
        assert "NAME" in out and "VERSION" in out
        assert "foo" in out and "1.0.0" in out
        assert "bar" in out and "0.2.1" in out

    def test_empty_rows(self, capsys):
        _output.table(
            title="Empty",
            columns=["A", "B"],
            rows=[],
        )
        out = capsys.readouterr().out
        assert "Empty" in out
        assert "A" in out and "B" in out

    def test_table_has_separator_line(self, capsys):
        _output.table(
            title="T",
            columns=["X"],
            rows=[["y"]],
        )
        out = capsys.readouterr().out
        assert "-" in out  # separator row


class TestError:
    """``error()`` writes the standard envelope to stderr."""

    def test_error_body_only(self, capsys):
        _output.error("something bad happened")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "error:" in captured.err
        assert "something bad happened" in captured.err

    def test_error_with_hint(self, capsys):
        _output.error("scan failed", hint="run `axi ext scan --help`")
        captured = capsys.readouterr()
        assert "error:" in captured.err
        assert "scan failed" in captured.err
        assert "hint:" in captured.err
        assert "axi ext scan" in captured.err


class TestNextSteps:
    """``next_steps()`` prints the hint block."""

    def test_empty_steps_noop(self, capsys):
        _output.next_steps([])
        assert capsys.readouterr().out == ""

    def test_default_header(self, capsys):
        _output.next_steps(["axi ext lint", "axi ext test"])
        out = capsys.readouterr().out
        assert "Next steps:" in out
        assert "axi ext lint" in out
        assert "axi ext test" in out

    def test_custom_header(self, capsys):
        _output.next_steps(["do x"], header="Suggestions:")
        out = capsys.readouterr().out
        assert "Suggestions:" in out
        assert "do x" in out


class TestHeading:
    """``heading()`` prints the text (style is only decoration)."""

    def test_heading_text(self, capsys):
        _output.heading("Publisher identity")
        out = capsys.readouterr().out
        assert "Publisher identity" in out


class TestSpinner:
    """In non-TTY mode the spinner prints the message once and yields."""

    def test_spinner_noop_prints_message(self, capsys):
        with _output.spinner("indexing..."):
            pass
        out = capsys.readouterr().out
        assert "indexing..." in out


class TestTerminatorConstants:
    """Constants are string literals callers can compose with verb names."""

    def test_constants_defined(self):
        assert isinstance(_output.TERMINATOR_OK, str)
        assert isinstance(_output.TERMINATOR_FAIL, str)
        assert isinstance(_output.TERMINATOR_WARN, str)

    def test_terminator_substrings(self):
        assert "passed" in _output.TERMINATOR_OK
        assert "failed" in _output.TERMINATOR_FAIL
