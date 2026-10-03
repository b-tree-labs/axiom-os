# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext scan`` — baseline pre-publish security/policy checks.

v0.1 scope (spec §10.2): manifest sanity, license allowlist, secrets heuristic,
dangerous-primitive heuristic, manifest/pyproject alignment. Behavioral
classification is Tier 4 and out of scope here.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from axiom.cli.ext.commands.scan import ScanProvider, scan_extension
from axiom.cli.ext.provider import CliContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(
    ext_path: Path, *argv: str, capsys=None
) -> tuple[int, str]:
    provider = ScanProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(ext_path), *argv])
    ctx = CliContext(cwd=ext_path.parent)
    if capsys is not None:
        capsys.readouterr()
    rc = provider.run(args, ctx)
    out = capsys.readouterr().out if capsys is not None else ""
    return rc, out


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _unalign_entry_points(ext: Path) -> None:
    """Break the scaffold's manifest/pyproject alignment by dropping the
    placeholder entry point. Tests that want to trigger the alignment
    failure call this on a fresh scaffold.
    """
    pyproj = ext / "pyproject.toml"
    text = pyproj.read_text()
    line = f'{ext.name} = "{ext.name}.commands.placeholder:cli"\n'
    text = text.replace(line, "", 1)
    pyproj.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_scan_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "scan" in providers
    assert providers["scan"].verb == "scan"


# ---------------------------------------------------------------------------
# Manifest sanity (hard fail)
# ---------------------------------------------------------------------------


def test_scan_happy_path_passes_on_fresh_scaffold(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("clean_ext")
    rc, out = _run_cli(ext, capsys=capsys)
    assert rc == 0, out
    assert "manifest_sanity" in out
    assert "PASS" in out or "✓" in out


def test_scan_fails_when_manifest_entry_missing_in_pyproject(
    scaffolded_extension, capsys
) -> None:
    # If the manifest declares a provides block but pyproject is missing the
    # matching entry point, scan must surface that as a hard failure so
    # authors close the gap before publishing.
    ext = scaffolded_extension("gap_ext")
    _unalign_entry_points(ext)
    rc, out = _run_cli(ext, capsys=capsys)
    assert rc == 1, out
    assert "manifest_pyproject_alignment" in out.lower() or "alignment" in out.lower()


def test_scan_hard_fails_on_missing_manifest(tmp_path: Path, capsys) -> None:
    ext = tmp_path / "brokenext"
    ext.mkdir()
    rc, out = _run_cli(ext, capsys=capsys)
    assert rc == 1
    assert "manifest" in out.lower()


# ---------------------------------------------------------------------------
# License allowlist
# ---------------------------------------------------------------------------


def test_scan_fails_on_nonstandard_license(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("restricted_ext")
    manifest = ext / "axiom-extension.toml"
    text = manifest.read_text()
    manifest.write_text(
        text.replace('license = "Apache-2.0"', 'license = "Proprietary-1.0"'),
        encoding="utf-8",
    )
    rc, out = _run_cli(ext, capsys=capsys)
    assert rc == 1
    assert "Proprietary-1.0" in out or "license" in out.lower()


def test_scan_accepts_overridden_license_via_allow_flag(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("override_ext")
    manifest = ext / "axiom-extension.toml"
    text = manifest.read_text()
    manifest.write_text(
        text.replace('license = "Apache-2.0"', 'license = "LicenseRef-Custom"'),
        encoding="utf-8",
    )
    rc, out = _run_cli(
        ext, "--allow-license", "LicenseRef-Custom", capsys=capsys
    )
    assert rc == 0, out


def test_scan_accepts_all_default_allowlisted_licenses(
    scaffolded_extension, capsys
) -> None:
    for spdx in ("Apache-2.0", "MIT", "BSD-3-Clause", "BSD-2-Clause", "MPL-2.0"):
        ext = scaffolded_extension(f"lic_{spdx.lower().replace('-', '_').replace('.', '_')}")
        manifest = ext / "axiom-extension.toml"
        text = manifest.read_text()
        manifest.write_text(
            text.replace('license = "Apache-2.0"', f'license = "{spdx}"'),
            encoding="utf-8",
        )
        rc, _ = _run_cli(ext, capsys=capsys)
        assert rc == 0, spdx


# ---------------------------------------------------------------------------
# Secrets heuristic (warning → 0; --strict → 1)
# ---------------------------------------------------------------------------


def test_scan_warns_on_obvious_secret_assignment(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("leaky_ext")
    _write(
        ext / "leaky_ext" / "commands" / "placeholder.py",
        "# Placeholder\n"
        "AWS_SECRET_ACCESS_KEY = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'\n",
    )
    rc, out = _run_cli(ext, capsys=capsys)
    # Warnings exit 0 unless --strict.
    assert rc == 0, out
    assert "WARN" in out or "warning" in out.lower()


def test_scan_strict_bumps_warning_to_failure(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("leaky_strict_ext")
    _write(
        ext / "leaky_strict_ext" / "commands" / "placeholder.py",
        "sk_test = 'sk-ABCDEF0123456789abcdef0123456789abcdef0123456789'\n",
    )
    rc, out = _run_cli(ext, "--strict", capsys=capsys)
    assert rc == 1, out


def test_scan_detects_private_key_block(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("pem_ext")
    _write(
        ext / "pem_ext" / "commands" / "placeholder.py",
        '"""-----BEGIN RSA PRIVATE KEY-----\nmock\n-----END RSA PRIVATE KEY-----"""\n',
    )
    rc, out = _run_cli(ext, "--strict", capsys=capsys)
    assert rc == 1


# ---------------------------------------------------------------------------
# Dangerous-primitive heuristic
# ---------------------------------------------------------------------------


def test_scan_warns_on_exec_in_public_api(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("exec_ext")
    _write(
        ext / "exec_ext" / "commands" / "placeholder.py",
        "def do(code):\n    exec(code)\n",
    )
    rc, out = _run_cli(ext, capsys=capsys)
    assert rc == 0  # warning
    assert "exec" in out.lower() or "WARN" in out


def test_scan_ignores_dangerous_primitive_in_internal(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("internal_ext")
    _write(
        ext / "internal_ext" / "_internal" / "util.py",
        "def do(code):\n    exec(code)\n",
    )
    # _internal/ is out of the public API surface — should not trigger.
    rc, out = _run_cli(ext, capsys=capsys)
    assert rc == 0, out
    # Scan may still report the dangerous_primitives check as a PASS line;
    # we specifically want no WARN for an _internal/ exec.
    assert "WARN" not in out or "exec" not in out


def test_scan_warns_on_subprocess_shell_true(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("shell_ext")
    _write(
        ext / "shell_ext" / "commands" / "placeholder.py",
        "import subprocess\n"
        "def do():\n    subprocess.run('ls', shell=True)\n",
    )
    rc, out = _run_cli(ext, capsys=capsys)
    assert rc == 0  # warning
    assert "shell" in out.lower()


# ---------------------------------------------------------------------------
# Manifest/pyproject alignment
# ---------------------------------------------------------------------------


def test_scan_fails_when_name_mismatches_pyproject(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("align_ext")
    pyproj = ext / "pyproject.toml"
    pyproj.write_text(
        pyproj.read_text().replace('name = "align_ext"', 'name = "different_name"'),
        encoding="utf-8",
    )
    rc, out = _run_cli(ext, capsys=capsys)
    assert rc == 1
    assert "align" in out.lower() or "mismatch" in out.lower() or "name" in out.lower()


def test_scan_fails_when_version_mismatches_pyproject(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("version_ext")
    pyproj = ext / "pyproject.toml"
    pyproj.write_text(
        pyproj.read_text().replace('version = "0.1.0"', 'version = "9.9.9"'),
        encoding="utf-8",
    )
    rc, out = _run_cli(ext, capsys=capsys)
    assert rc == 1


def test_scan_fails_when_provides_block_has_no_entry_point(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("ep_ext")
    # Strip [project.entry-points."axiom.commands"] so the placeholder cmd
    # block in the manifest has no matching ep.
    pyproj_path = ext / "pyproject.toml"
    text = pyproj_path.read_text()
    text = re.sub(
        r'\[project\.entry-points\."axiom\.commands"\]\n[^\[]*',
        "",
        text,
        count=1,
    )
    pyproj_path.write_text(text, encoding="utf-8")
    rc, out = _run_cli(ext, capsys=capsys)
    assert rc == 1


# ---------------------------------------------------------------------------
# --json output
# ---------------------------------------------------------------------------


def test_scan_json_output_shape(scaffolded_extension, capsys) -> None:
    ext = scaffolded_extension("jsonshape_ext")
    rc, out = _run_cli(ext, "--json", capsys=capsys)
    assert rc == 0
    data = json.loads(out)
    assert "extension" in data
    assert "checks" in data
    assert isinstance(data["checks"], list)
    # Every check must have the standard keys.
    for check in data["checks"]:
        assert "check" in check
        assert "severity" in check
        assert check["severity"] in {"pass", "warn", "fail"}
        assert "detail" in check


# ---------------------------------------------------------------------------
# scan_extension() direct invocation returns structured results
# ---------------------------------------------------------------------------


def test_scan_extension_returns_results(
    scaffolded_extension,
) -> None:
    ext = scaffolded_extension("direct_ext")
    results = scan_extension(ext)
    assert results.checks
    assert results.hard_failure is False
    assert {c.check for c in results.checks}.issuperset(
        {"manifest_sanity", "license", "secrets", "dangerous_primitives", "manifest_pyproject_alignment"}
    )
