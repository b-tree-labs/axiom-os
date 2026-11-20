# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""End-to-end meta-tests: spin up pytest inside pytest, run the base classes
against a synthesized on-disk extension, and verify outcomes.

These tests also exercise the ``@pytest.fixture`` fixture bodies (e.g.
``extension_manifest``, ``extension_root``, ``pyproject``) that the direct-
call tests cannot reach because pytest 9 forbids calling fixture methods.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

# conftest already enables pytester via ``pytest_plugins``.


GOOD_MANIFEST = (
    textwrap.dedent(
        """
    [extension]
    name = "demo_ext"
    version = "0.1.0"
    description = "pytester-integration demo"
    license = "Apache-2.0"
    aeos_version = "0.1.0"

    [[extension.provides]]
    kind = "tool"
    name = "demo_tool"
    entry = "demo_ext.tools.demo:DemoTool"
    description = "Demo tool"
    idempotent = true
    side_effects = "none"
    """
    ).strip()
    + "\n"
)


GOOD_PYPROJECT = (
    textwrap.dedent(
        """
    [project]
    name = "demo_ext"
    version = "0.1.0"
    description = "demo"
    """
    ).strip()
    + "\n"
)


def _write_demo_extension(root: Path) -> None:
    (root / "axiom-extension.toml").write_text(GOOD_MANIFEST, encoding="utf-8")
    (root / "pyproject.toml").write_text(GOOD_PYPROJECT, encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("# CHANGELOG\n", encoding="utf-8")
    (root / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    pkg = root / "demo_ext"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text('"""demo."""\n__all__ = []\n', encoding="utf-8")


def test_extension_standard_end_to_end(pytester) -> None:  # type: ignore[no-untyped-def]
    """Drive the full ``ExtensionStandardTests`` suite inside a nested pytest."""
    ext_root = pytester.path / "ext"
    ext_root.mkdir()
    _write_demo_extension(ext_root)

    pytester.makepyfile(
        test_demo=f"""
        from pathlib import Path
        import pytest
        from axiom_tests.unit_tests import ExtensionStandardTests

        class TestDemoExtension(ExtensionStandardTests):
            @pytest.fixture
            def extension_manifest_path(self) -> Path:
                return Path(r"{ext_root}") / "axiom-extension.toml"
        """
    )
    result = pytester.runpytest("-q")
    result.assert_outcomes(passed=9, skipped=2)


def test_extension_standard_catches_missing_file(pytester) -> None:  # type: ignore[no-untyped-def]
    ext_root = pytester.path / "broken"
    ext_root.mkdir()
    _write_demo_extension(ext_root)
    (ext_root / "CHANGELOG.md").unlink()

    pytester.makepyfile(
        test_broken=f"""
        from pathlib import Path
        import pytest
        from axiom_tests.unit_tests import ExtensionStandardTests

        class TestBroken(ExtensionStandardTests):
            @pytest.fixture
            def extension_manifest_path(self) -> Path:
                return Path(r"{ext_root}") / "axiom-extension.toml"
        """
    )
    result = pytester.runpytest("-q")
    # Expect exactly one assertion failure: the missing-files check.
    result.assert_outcomes(passed=8, failed=1, skipped=2)


def test_extension_standard_rejects_missing_aeos_version(pytester) -> None:  # type: ignore[no-untyped-def]
    ext_root = pytester.path / "noaeos"
    ext_root.mkdir()
    _write_demo_extension(ext_root)
    manifest_path = ext_root / "axiom-extension.toml"
    manifest_path.write_text(
        GOOD_MANIFEST.replace('aeos_version = "0.1.0"\n', ""), encoding="utf-8"
    )

    pytester.makepyfile(
        test_noaeos=f"""
        from pathlib import Path
        import pytest
        from axiom_tests.unit_tests import ExtensionStandardTests

        class TestNoAeos(ExtensionStandardTests):
            @pytest.fixture
            def extension_manifest_path(self) -> Path:
                return Path(r"{ext_root}") / "axiom-extension.toml"
        """
    )
    result = pytester.runpytest("-q")
    # Two failures: schema validation (aeos_version is required) and
    # the dedicated aeos_version check.
    assert result.ret != 0
    stdout = result.stdout.str()
    assert "aeos_version" in stdout


def test_plugin_loads_in_fresh_session(pytester) -> None:  # type: ignore[no-untyped-def]
    """Fixtures are available in a consumer with no conftest at all."""
    pytester.makepyfile(
        test_mock_llm_available="""
        def test_llm(mock_llm):
            mock_llm.queue("hi")
            assert mock_llm.complete("p") == "hi"

        def test_oidc(mock_oidc):
            tok = mock_oidc.issue(subject="@a:b")
            assert mock_oidc.is_valid(tok.encode(), audience="axiom")

        def test_home(tmp_axiom_home):
            assert tmp_axiom_home.exists()
        """
    )
    result = pytester.runpytest("-q")
    result.assert_outcomes(passed=3)
