# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.setup.renderer."""


import pytest

from axiom.setup.renderer import (
    JARGON_MAP,
    _c,
    _Colors,
    banner,
    blank,
    divider,
    error,
    friendly_name,
    heading,
    info,
    numbered_steps,
    progress_bar,
    prompt_choice,
    prompt_text,
    prompt_yn,
    set_color_enabled,
    status_line,
    success,
    text,
    warning,
)


@pytest.fixture(autouse=True)
def _disable_color():
    """Disable color for predictable test output."""
    set_color_enabled(False)
    yield
    set_color_enabled(False)


class TestJargonMap:
    def test_known_env_var(self):
        assert friendly_name("GITLAB_TOKEN") == "GitLab access key"

    def test_known_generic_term(self):
        assert friendly_name("API key") == "access key"

    def test_unknown_term_passes_through(self):
        assert friendly_name("UNKNOWN_THING") == "UNKNOWN_THING"

    def test_all_entries_are_strings(self):
        for k, v in JARGON_MAP.items():
            assert isinstance(k, str)
            assert isinstance(v, str)

    def test_ms_graph_entries(self):
        assert friendly_name("MS_GRAPH_CLIENT_ID") == "Microsoft 365 app ID"


class TestBanner:
    def test_banner_contains_mascot(self, capsys):
        # Default banner is the AXI character (chat-TUI ASCII variant)
        banner()
        out = capsys.readouterr().out
        assert "AXI" in out          # hull placard label
        assert "(o)" in out          # binocular eye pupils

    def test_banner_contains_ship_elements(self, capsys):
        # AXI banner has eye housings, body frame, treads
        banner()
        out = capsys.readouterr().out
        assert "+----+" in out        # eye housing top/bottom
        assert "+-----+-----+" in out # body top
        assert "+=+=+" in out         # tread housing
        assert friendly_name("MS_GRAPH_CLIENT_SECRET") == "Microsoft 365 app secret"
        assert friendly_name("MS_GRAPH_TENANT_ID") == "Microsoft 365 tenant ID"

    def test_custom_banner_via_branding(self, capsys):
        """When a domain product registers a banner_fn, banner() delegates to it."""
        from axiom.infra.branding import BrandingConfig, register, reset

        def _custom_banner() -> None:
            print("CUSTOM PRODUCT BANNER")

        try:
            register(BrandingConfig(cli_name="custom", product_name="Custom Product",
                                    banner_fn=_custom_banner))
            banner()
            out = capsys.readouterr().out
            assert "CUSTOM PRODUCT BANNER" in out
        finally:
            reset()


class TestColorHelper:
    def test_no_color_returns_plain(self):
        set_color_enabled(False)
        assert _c(_Colors.GREEN, "hello") == "hello"

    def test_color_returns_ansi(self):
        set_color_enabled(True)
        result = _c(_Colors.GREEN, "hello")
        assert "\033[32m" in result
        assert "hello" in result
        assert "\033[0m" in result
        set_color_enabled(False)


class TestDisplayPrimitives:
    def test_heading(self, capsys):
        heading("Test Section")
        out = capsys.readouterr().out
        assert "Test Section" in out

    def test_status_line_ok(self, capsys):
        status_line("Python", "3.11.0", True)
        out = capsys.readouterr().out
        assert "Python" in out
        assert "3.11.0" in out

    def test_status_line_not_ok(self, capsys):
        status_line("Git", "missing", False)
        out = capsys.readouterr().out
        assert "Git" in out

    def test_progress_bar(self, capsys):
        progress_bar(5, 10)
        out = capsys.readouterr().out
        assert "50%" in out

    def test_progress_bar_complete(self, capsys):
        progress_bar(10, 10)
        out = capsys.readouterr().out
        assert "100%" in out

    def test_progress_bar_zero_total(self, capsys):
        progress_bar(0, 0)
        out = capsys.readouterr().out
        assert out == ""

    def test_divider(self, capsys):
        divider()
        out = capsys.readouterr().out
        assert "─" in out

    def test_info(self, capsys):
        info("note")
        out = capsys.readouterr().out
        assert "note" in out

    def test_success(self, capsys):
        success("done")
        out = capsys.readouterr().out
        assert "done" in out

    def test_warning(self, capsys):
        warning("caution")
        out = capsys.readouterr().out
        assert "caution" in out

    def test_error(self, capsys):
        error("fail")
        out = capsys.readouterr().out
        assert "fail" in out

    def test_blank(self, capsys):
        blank()
        out = capsys.readouterr().out
        assert out == "\n"

    def test_text(self, capsys):
        text("hello world")
        out = capsys.readouterr().out
        assert "hello world" in out

    def test_numbered_steps(self, capsys):
        numbered_steps(["first", "second", "third"])
        out = capsys.readouterr().out
        assert "1." in out
        assert "first" in out
        assert "3." in out
        assert "third" in out


class TestInputPrimitives:
    def test_prompt_choice(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda _: "2")
        result = prompt_choice("Pick one:", ["Apple", "Banana", "Cherry"])
        assert result == 1  # 0-based

    def test_prompt_choice_eof(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError))
        result = prompt_choice("Pick one:", ["A", "B"])
        assert result == 0

    def test_prompt_yn_default_yes(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert prompt_yn("Continue?", default=True) is True

    def test_prompt_yn_explicit_no(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert prompt_yn("Continue?", default=True) is False

    def test_prompt_yn_eof(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError))
        assert prompt_yn("Continue?", default=False) is False

    def test_prompt_text_with_input(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "my value")
        assert prompt_text("Name") == "my value"

    def test_prompt_text_default(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert prompt_text("Name", default="default") == "default"

    def test_prompt_yn_ctrl_c_raises(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(KeyboardInterrupt))
        with pytest.raises(KeyboardInterrupt):
            prompt_yn("Continue?")

    def test_prompt_choice_ctrl_c_raises(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(KeyboardInterrupt))
        with pytest.raises(KeyboardInterrupt):
            prompt_choice("Pick:", ["A", "B"])

    def test_prompt_text_ctrl_c_raises(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(KeyboardInterrupt))
        with pytest.raises(KeyboardInterrupt):
            prompt_text("Name")
