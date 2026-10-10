# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for @axiom/design-tokens and its first consumer (webgate).

Run from the repo root with the workspace venv::

    python -m pytest packages/axiom-design-tokens/tests

Stdlib + pytest only. The build script is loaded from its file so the tests
exercise exactly what CI and ``npm run check`` execute.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_DIR.parents[1]
SCRIPT = PACKAGE_DIR / "scripts" / "build_tokens.py"
TOKENS_CSS = PACKAGE_DIR / "tokens.css"
TOKENS_JSON = PACKAGE_DIR / "tokens.json"
PRESET_JS = PACKAGE_DIR / "tailwind.preset.js"
WEBGATE_FRONTEND = REPO_ROOT / "src" / "axiom" / "extensions" / "builtins" / "webgate" / "frontend"
WEBGATE_CSS = WEBGATE_FRONTEND / "src" / "index.css"
WEBGATE_TAILWIND = WEBGATE_FRONTEND / "tailwind.config.js"

THEME_ORDER = ("base", "dark", "light")
VAR_REF_RE = re.compile(r"var\(\s*(--[\w-]+)")
VAR_DEF_RE = re.compile(r"(--[\w-]+)\s*:")
COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
# JS block and line comments; the sources scanned here hold no string with "//".
JS_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
# Python tooling is not shipped and quotes the forbidden patterns as literals.
UNSHIPPED_DIRS = {"scripts", "tests", "__pycache__", ".pytest_cache", "node_modules"}


def _load_build_tokens():
    spec = importlib.util.spec_from_file_location("axiom_design_tokens_build", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shipped_package_files() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE_DIR.rglob("*")
        if path.is_file() and UNSHIPPED_DIRS.isdisjoint(path.relative_to(PACKAGE_DIR).parts)
    )


@pytest.fixture(scope="module")
def build_tokens():
    return _load_build_tokens()


@pytest.fixture(scope="module")
def css_text() -> str:
    return TOKENS_CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def root_tokens(build_tokens, css_text) -> dict[str, str]:
    """Every custom property declared on :root, the unstamped fallback."""
    doc = build_tokens.build(css_text)
    return {**doc["shared"], **doc["themes"]["base"]}


# ---------------------------------------------------------------------------
# scripts/build_tokens.py
# ---------------------------------------------------------------------------


class TestBuildScript:
    def test_check_mode_passes_for_the_committed_files(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_regenerating_tokens_json_is_a_no_op(self, build_tokens, css_text):
        expected = build_tokens.render(build_tokens.build(css_text))
        assert TOKENS_JSON.read_text(encoding="utf-8") == expected

    def test_check_mode_reports_drift(self, tmp_path, build_tokens, css_text):
        css = tmp_path / "tokens.css"
        css.write_text(css_text, encoding="utf-8")
        doc = build_tokens.build(css_text)
        doc["themes"]["light"]["--theme-page-bg"] = "#000000"
        stale = tmp_path / "tokens.json"
        stale.write_text(build_tokens.render(doc), encoding="utf-8")
        rc = build_tokens.main(["--check", "--css", str(css), "--json", str(stale)])
        assert rc == 1

    def test_rejects_var_reference_without_root_definition(self, build_tokens, css_text):
        broken = css_text.replace(
            "--accent: var(--theme-accent);", "--accent: var(--theme-missing);", 1
        )
        assert broken != css_text
        with pytest.raises(build_tokens.TokenError, match="--theme-missing"):
            build_tokens.build(broken)

    def test_rejects_light_before_dark(self, build_tokens, css_text):
        swapped = (
            css_text.replace("[data-theme='dark']", "[data-theme='TMP']")
            .replace("[data-theme='light']", "[data-theme='dark']")
            .replace("[data-theme='TMP']", "[data-theme='light']")
        )
        with pytest.raises(build_tokens.TokenError, match="light last"):
            build_tokens.build(swapped)

    def test_rejects_theme_name_that_root_does_not_declare(self, build_tokens, css_text):
        extra = "  --theme-brand-new: #123456;\n"
        broken = css_text.replace(
            "[data-theme='dark'] {\n", "[data-theme='dark'] {\n" + extra, 1
        ).replace("[data-theme='light'] {\n", "[data-theme='light'] {\n" + extra, 1)
        with pytest.raises(build_tokens.TokenError, match="--theme-brand-new"):
            build_tokens.build(broken)

    def test_rejects_anything_that_fetches(self, build_tokens, css_text):
        fetching = css_text.replace(
            "--theme-nav-active-fg: #1a1d21;",
            "--theme-nav-active-fg: url(https://example.invalid/x.png);",
            1,
        )
        assert fetching != css_text
        with pytest.raises(build_tokens.TokenError, match="network"):
            build_tokens.build(fetching)

    def test_rejects_component_rules_in_the_token_file(self, build_tokens, css_text):
        with pytest.raises(build_tokens.TokenError, match="exactly these blocks"):
            build_tokens.build(css_text + "\n.btn { color: red; }\n")


# ---------------------------------------------------------------------------
# tokens.css
# ---------------------------------------------------------------------------


class TestTokensCss:
    def test_blocks_are_root_then_dark_then_light(self, build_tokens, css_text):
        selectors = [selector for selector, _ in build_tokens.parse_blocks(css_text)]
        assert selectors == [":root", "[data-theme='dark']", "[data-theme='light']"]

    def test_light_block_comes_after_dark_block(self, css_text):
        body = COMMENT_RE.sub("", css_text)
        dark = re.search(r"^\[data-theme='dark'\]\s*\{", body, re.MULTILINE)
        light = re.search(r"^\[data-theme='light'\]\s*\{", body, re.MULTILINE)
        assert dark and light, "both explicit theme blocks must exist"
        assert body.count("[data-theme='light']") == 1
        assert light.start() > dark.start(), "light must be last to win at equal specificity"

    def test_every_variable_referenced_in_tokens_css_is_defined(self, css_text, root_tokens):
        referenced = set(VAR_REF_RE.findall(css_text))
        assert referenced, "aliases are expected to reference the themed palette"
        assert not referenced - set(root_tokens)

    def test_dark_matches_the_unstamped_root(self, build_tokens, css_text):
        # The explicit dark selector exists only to be explicit; it must not
        # diverge from the fallback or an unstamped page would look different.
        doc = build_tokens.build(css_text)
        assert doc["themes"]["dark"] == doc["themes"]["base"]


# ---------------------------------------------------------------------------
# tokens.json
# ---------------------------------------------------------------------------


class TestTokensJson:
    @pytest.fixture(scope="class")
    def doc(self) -> dict:
        return json.loads(TOKENS_JSON.read_text(encoding="utf-8"))

    def test_three_theme_maps_with_identical_key_sets(self, doc):
        assert tuple(doc["themes"]) == THEME_ORDER
        key_sets = [set(doc["themes"][theme]) for theme in THEME_ORDER]
        assert key_sets[0], "theme maps must not be empty"
        assert key_sets[0] == key_sets[1] == key_sets[2]

    def test_shared_tokens_are_disjoint_from_themed_ones(self, doc):
        assert set(doc["shared"]).isdisjoint(doc["themes"]["base"])
        assert {"--app-font-sans", "--button-radius", "--focus-ring"} <= set(doc["shared"])
        assert "--theme-accent" in doc["themes"]["base"]

    def test_json_equals_the_root_declarations_in_css(self, doc, root_tokens):
        assert {**doc["shared"], **doc["themes"]["base"]} == root_tokens

    def test_metadata_points_back_at_the_source(self, doc):
        assert doc["source"] == "tokens.css"
        assert doc["order"] == list(THEME_ORDER)
        assert doc["selectors"] == {
            "base": ":root",
            "dark": "[data-theme='dark']",
            "light": "[data-theme='light']",
        }


# ---------------------------------------------------------------------------
# consumers: webgate's remaining index.css, tailwind.config.js, the preset
# ---------------------------------------------------------------------------


class TestConsumers:
    def test_every_variable_used_by_webgate_css_is_defined(self, root_tokens):
        used = {
            name
            for name in VAR_REF_RE.findall(WEBGATE_CSS.read_text(encoding="utf-8"))
            if not name.startswith("--tw-")  # Tailwind's own ring/shadow plumbing
        }
        assert "--theme-accent" in used
        missing = sorted(used - set(root_tokens))
        assert not missing, f"webgate references undefined tokens: {missing}"

    def test_every_variable_used_by_the_preset_is_defined(self, root_tokens):
        code = JS_COMMENT_RE.sub("", PRESET_JS.read_text(encoding="utf-8"))
        used = set(VAR_REF_RE.findall(code))
        assert "--theme-page-bg" in used
        missing = sorted(used - set(root_tokens))
        assert not missing, f"preset references undefined tokens: {missing}"

    def test_webgate_css_defines_no_tokens_itself(self):
        body = COMMENT_RE.sub("", WEBGATE_CSS.read_text(encoding="utf-8"))
        defined = {name for name in VAR_DEF_RE.findall(body) if not name.startswith("--tw-")}
        assert not defined, f"token definitions crept back into webgate: {sorted(defined)}"
        assert not re.search(r"^\s*:root\s*\{", body, re.MULTILINE)
        assert "[data-theme=" not in body

    def test_webgate_css_imports_the_package_tokens_first(self):
        body = COMMENT_RE.sub("", WEBGATE_CSS.read_text(encoding="utf-8")).lstrip()
        match = re.match(r"@import\s+['\"]([^'\"]+)['\"]\s*;", body)
        assert match, "@import of the tokens must be the first statement"
        target = (WEBGATE_CSS.parent / match.group(1)).resolve()
        assert target == TOKENS_CSS.resolve()

    def test_webgate_tailwind_config_uses_the_preset(self):
        code = JS_COMMENT_RE.sub("", WEBGATE_TAILWIND.read_text(encoding="utf-8"))
        match = re.search(r"from\s+['\"]([^'\"]*tailwind\.preset\.js)['\"]", code)
        assert match, "tailwind.config.js must import the preset"
        assert (WEBGATE_FRONTEND / match.group(1)).resolve() == PRESET_JS.resolve()
        assert re.search(r"presets:\s*\[", code)
        assert "var(--theme-" not in code, "the colour mapping belongs to the preset now"


# ---------------------------------------------------------------------------
# zero external requests (air-gapped sites)
# ---------------------------------------------------------------------------

EXTERNAL_IMPORT_RE = re.compile(r"@import\s+(?:url\()?\s*['\"]?\s*(?:https?:)?//", re.IGNORECASE)


@pytest.mark.parametrize("path", [*_shipped_package_files(), WEBGATE_CSS], ids=lambda p: p.name)
def test_zero_external_requests(path: Path):
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        lowered = line.lower()
        for needle in ("http://", "https://", "url("):
            assert needle not in lowered, (
                f"{path.name}:{lineno} contains {needle!r}: {line.strip()}"
            )
        assert not EXTERNAL_IMPORT_RE.search(line), f"{path.name}:{lineno} imports an external host"
