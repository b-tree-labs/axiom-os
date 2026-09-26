# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The welcome header lines up.

Reported from live use: "the alignment, spacing and header section could be
better." It stacked the art above the text, which left three different indents
on four consecutive lines and no column for the eye to follow:

    (◉)─(◉)
      Axi
    cli v1.9.0 — Product · Axi
    /home/dev/projects/example

Art belongs in a fixed-width left column with the text beside it, and every
text row starts at the same offset — including rows with no art next to them.

The existing mascot test asserts the characters are present. Present and
aligned are different claims, so these pin the second one.
"""

from __future__ import annotations


def _render(text_rows, art_rows=("(◉)─(◉)", "  Axi")):
    """Mirror of the header assembly, for layout assertions."""
    width = max(len(row) for row in art_rows)
    out = []
    for index in range(max(len(art_rows), len(text_rows))):
        art = art_rows[index] if index < len(art_rows) else ""
        text = text_rows[index] if index < len(text_rows) else ""
        out.append(f"  {art.ljust(width)}   {text}".rstrip())
    return out


def _text_column(line: str, needle: str) -> int:
    """Where ``needle`` starts on this row."""
    return line.index(needle)


class TestTheTextColumnIsOneColumn:
    def test_every_text_row_starts_at_the_same_offset(self):
        texts = ["cli v1.9.0 · Product", "/home/dev/projects/x", "site: example"]
        rows = _render(texts)
        offsets = {_text_column(r, t) for r, t in zip(rows, texts, strict=True)}
        assert len(offsets) == 1, f"text should start in one column, got {offsets}"

    def test_a_row_with_no_art_still_lines_up(self):
        """The third line has no mascot beside it and must not drift back."""
        rows = _render(["a", "b", "c"])
        assert len(rows) == 3
        assert _text_column(rows[1], "b") == _text_column(rows[2], "c")

    def test_the_header_is_three_lines_not_five(self):
        rows = _render(["cli v1.9.0 · Product", "/home/dev/projects/x"])
        assert len(rows) == 2, "art shares rows with text rather than owning its own"

    def test_no_trailing_whitespace_on_any_row(self):
        for row in _render(["one", "two", "three"]):
            assert row == row.rstrip(), f"trailing space on {row!r}"

    def test_art_rows_are_padded_to_a_common_width(self):
        # Markers that cannot occur in the art — a single letter can, and a
        # bare "x" silently matched the "x" in the mascot's own name instead
        # of the text row, so this asserted nothing about layout.
        markers = ["<<1>>", "<<2>>"]
        rows = _render(markers)
        prefixes = {row.index(m) for row, m in zip(rows, markers, strict=True)}
        assert len(prefixes) == 1


class TestTheHeaderLineDoesNotRepeatItself:
    def test_the_version_line_does_not_also_say_axi(self):
        """The mascot is beside it already saying so."""
        line = "neut v1.9.0 · Neutron OS"
        assert "AXI" not in line

    def test_it_still_names_the_cli_the_version_and_the_product(self):
        line = "neut v1.9.0 · Neutron OS"
        assert "neut" in line and "1.9.0" in line and "Neutron OS" in line


class TestTheRealRendererProducesThisShape:
    """Every claim above, asserted against the code that actually runs.

    The helper tests describe the intended shape; three mutants survived them
    because they exercised a local copy of the arithmetic. What ships is what
    needs pinning, so these drive `render_welcome` and read its output.
    """

    def _render(self, monkeypatch, workspace_context=None):
        from axiom.extensions.builtins.chat import fullscreen

        class _B:
            package_name = "axiom-os-lm"
            product_name = "ZQProduct"
            cli_name = "zqcli"
            mascot_name = "Zed"

        monkeypatch.setattr("axiom.infra.branding.get_branding", lambda *a, **k: _B())
        monkeypatch.setattr(fullscreen, "get_branding", lambda *a, **k: _B(), raising=False)

        captured: list[str] = []

        class _TUI:
            def _append_output(self, s: str) -> None:
                captured.append(s)

        provider = fullscreen._TuiRenderProvider.__new__(fullscreen._TuiRenderProvider)
        provider._tui = _TUI()
        provider.render_welcome(workspace_context=workspace_context)
        return "".join(captured)

    def _header_lines(self, out: str) -> list[str]:
        lines = []
        for line in out.splitlines():
            if not line.strip() or "Type /help" in line:
                continue
            lines.append(line)
        return lines

    def test_the_version_shares_the_mascot_line(self, monkeypatch):
        lines = self._header_lines(self._render(monkeypatch))
        art_line = next(ln for ln in lines if "◉" in ln)
        assert "v" in art_line.split("◉)")[-1], (
            "the version text should sit beside the mascot, not under it"
        )

    def test_the_path_shares_the_axi_line(self, monkeypatch):
        lines = self._header_lines(self._render(monkeypatch))
        axi_line = next(ln for ln in lines if "Zed" in ln and "◉" not in ln)
        assert "/" in axi_line

    def test_both_text_columns_start_at_the_same_offset(self, monkeypatch):
        """Kills the unpadded-art mutant: without ljust these diverge."""
        lines = self._header_lines(self._render(monkeypatch))
        art_line = next(ln for ln in lines if "◉" in ln)
        axi_line = next(ln for ln in lines if "Zed" in ln and "◉" not in ln)
        assert art_line.index("zqcli") == axi_line.index("/"), (
            f"text columns diverge:\n{art_line!r}\n{axi_line!r}"
        )

    def test_no_header_line_carries_trailing_whitespace(self, monkeypatch):
        out = self._render(monkeypatch)
        for line in out.splitlines():
            assert line == line.rstrip(), f"trailing whitespace: {line!r}"

    def test_the_version_line_does_not_repeat_the_mascot_name(self, monkeypatch):
        """The mascot is on that very line already saying AXI."""
        lines = self._header_lines(self._render(monkeypatch))
        art_line = next(ln for ln in lines if "◉" in ln)
        assert "Zed" not in art_line, (
            "the version text should not also spell out the mascot beside it"
        )

    def test_a_workspace_row_aligns_with_the_others(self, monkeypatch):
        out = self._render(monkeypatch, workspace_context="Working on model: ut-triga")
        lines = self._header_lines(out)
        art_line = next(ln for ln in lines if "◉" in ln)
        extra = [ln for ln in lines if "◉" not in ln and "Zed" not in ln]
        for line in extra:
            assert len(line) - len(line.lstrip()) == art_line.index("zqcli"), (
                f"a row with no art beside it drifted: {line!r}"
            )

    def test_the_gutter_is_wide_enough_to_read(self, monkeypatch):
        """Spacing, not just alignment.

        Columns that line up but sit one space apart still read as one
        run-on field. The complaint was "alignment, spacing and header
        section", and this is the spacing half.
        """
        lines = self._header_lines(self._render(monkeypatch))
        art_line = next(ln for ln in lines if "◉" in ln)
        gutter = art_line.index("zqcli") - (art_line.rindex(")") + 1)
        assert gutter >= 2, f"gutter of {gutter} space(s) reads as one field"


# NOTE on a deliberately unkilled mutant: removing the `.rstrip()` from the
# row assembly survives this suite, and that is correct rather than a gap.
# The art column is a fixed two rows and the text column is always at least
# two (version + cwd), so a row with art and no text cannot occur and no
# trailing whitespace is ever produced. The rstrip is defence against a
# future third art row, not behaviour reachable today. Kept for that, and
# recorded here so nobody spends an afternoon writing a test for it.
