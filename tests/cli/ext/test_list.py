# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext list`` — unified pip + axi view."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from axiom.cli.ext.commands.list import (
    ListProvider,
    ListRow,
    _PipEntry,
    build_rows,
)
from axiom.cli.ext.install_state import InstallRecord, record_install
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    monkeypatch.delenv("AXIOM_REGISTRY_URL", raising=False)
    return home


def _fake_record(
    name: str = "greeter",
    version: str = "0.1.0",
    install_path: str | None = None,
) -> InstallRecord:
    return InstallRecord(
        name=name,
        version=version,
        installed_at="2026-04-22T12:00:00Z",
        install_path=install_path or f"/tmp/fake/{name}-{version}",
        artifact_sha256="deadbeef",
        signature_sha256="cafebabe",
        registry_url="file:///tmp/registry",
    )


def _run_list_cli(
    *argv: str,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
    pip_entries: list[_PipEntry] | None = None,
) -> tuple[int, str]:
    """Invoke the provider with a stubbed pip source."""
    if pip_entries is not None:
        monkeypatch.setattr(
            "axiom.cli.ext.commands.list._pip_source", lambda: list(pip_entries)
        )
    provider = ListProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(list(argv))
    ctx = CliContext(cwd=Path.cwd())
    capsys.readouterr()
    rc = provider.run(args, ctx)
    out = capsys.readouterr().out
    return rc, out


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_list_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "list" in providers
    assert providers["list"].verb == "list"


# ---------------------------------------------------------------------------
# Core row-building
# ---------------------------------------------------------------------------


def test_build_rows_empty_when_both_sources_empty(axiom_home: Path) -> None:
    rows = build_rows(pip_source=lambda: [], axi_source=lambda: [])
    assert rows == []


def test_build_rows_pip_only(axiom_home: Path) -> None:
    pip = [_PipEntry(name="alpha", version="0.1.0", enabled=True)]
    rows = build_rows(pip_source=lambda: pip, axi_source=lambda: [])
    assert rows == [
        ListRow(name="alpha", version="0.1.0", source="pip", status="enabled")
    ]


def test_build_rows_axi_only_installed(axiom_home: Path, tmp_path: Path) -> None:
    target = tmp_path / "greeter-0.1.0"
    target.mkdir()
    rec = _fake_record(install_path=str(target))
    rows = build_rows(pip_source=lambda: [], axi_source=lambda: [rec])
    assert rows == [
        ListRow(name="greeter", version="0.1.0", source="axi", status="installed")
    ]


def test_build_rows_axi_only_missing_when_path_gone(
    axiom_home: Path, tmp_path: Path
) -> None:
    rec = _fake_record(install_path=str(tmp_path / "ghost"))
    rows = build_rows(pip_source=lambda: [], axi_source=lambda: [rec])
    assert rows[0].status == "missing"


def test_build_rows_both_sources_merge(axiom_home: Path, tmp_path: Path) -> None:
    target = tmp_path / "greeter-0.1.0"
    target.mkdir()
    pip = [_PipEntry(name="greeter", version="0.1.0", enabled=True)]
    rec = _fake_record(install_path=str(target))
    rows = build_rows(pip_source=lambda: pip, axi_source=lambda: [rec])
    assert len(rows) == 1
    assert rows[0].source == "both"
    assert rows[0].status == "enabled"


def test_build_rows_pip_disabled_status_propagates(axiom_home: Path) -> None:
    pip = [_PipEntry(name="alpha", version="0.1.0", enabled=False)]
    rows = build_rows(pip_source=lambda: pip, axi_source=lambda: [])
    assert rows[0].status == "disabled"


def test_build_rows_source_filter_pip(axiom_home: Path) -> None:
    pip = [_PipEntry(name="alpha", version="0.1.0", enabled=True)]
    axi = [_fake_record("greeter", "0.1.0")]
    rows = build_rows(
        pip_source=lambda: pip, axi_source=lambda: axi, source_filter="pip"
    )
    names = [r.name for r in rows]
    assert "alpha" in names and "greeter" not in names


def test_build_rows_source_filter_axi(axiom_home: Path) -> None:
    pip = [_PipEntry(name="alpha", version="0.1.0", enabled=True)]
    axi = [_fake_record("greeter", "0.1.0")]
    rows = build_rows(
        pip_source=lambda: pip, axi_source=lambda: axi, source_filter="axi"
    )
    names = [r.name for r in rows]
    assert "greeter" in names and "alpha" not in names


def test_build_rows_source_filter_both_keeps_both_source(
    axiom_home: Path, tmp_path: Path
) -> None:
    target = tmp_path / "greeter-0.1.0"
    target.mkdir()
    pip = [_PipEntry(name="greeter", version="0.1.0", enabled=True)]
    axi = [_fake_record(install_path=str(target))]
    rows_pip = build_rows(
        pip_source=lambda: pip, axi_source=lambda: axi, source_filter="pip"
    )
    rows_axi = build_rows(
        pip_source=lambda: pip, axi_source=lambda: axi, source_filter="axi"
    )
    assert [r.name for r in rows_pip] == ["greeter"]
    assert [r.name for r in rows_axi] == ["greeter"]
    assert rows_pip[0].source == "both" == rows_axi[0].source


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_empty_shows_get_started_blurb(
    axiom_home: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out = _run_list_cli(
        capsys=capsys, monkeypatch=monkeypatch, pip_entries=[]
    )
    assert rc == 0
    assert "No extensions installed" in out
    assert "axi ext init" in out


def test_cli_json_shape(
    axiom_home: Path, capsys, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "greeter-0.1.0"
    target.mkdir()
    record_install(_fake_record(install_path=str(target)))
    rc, out = _run_list_cli(
        "--json",
        capsys=capsys,
        monkeypatch=monkeypatch,
        pip_entries=[_PipEntry(name="alpha", version="0.2.0", enabled=True)],
    )
    assert rc == 0
    data = json.loads(out)
    assert "extensions" in data
    names = [e["name"] for e in data["extensions"]]
    assert set(names) == {"alpha", "greeter"}
    for entry in data["extensions"]:
        assert set(entry.keys()) == {"name", "version", "source", "status"}


def test_cli_source_filter_pip(
    axiom_home: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_install(_fake_record("greeter", "0.1.0"))
    rc, out = _run_list_cli(
        "--json",
        "--source",
        "pip",
        capsys=capsys,
        monkeypatch=monkeypatch,
        pip_entries=[_PipEntry(name="alpha", version="0.2.0", enabled=True)],
    )
    assert rc == 0
    data = json.loads(out)
    names = [e["name"] for e in data["extensions"]]
    assert "greeter" not in names
    assert "alpha" in names


def test_cli_source_filter_axi(
    axiom_home: Path, capsys, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "greeter-0.1.0"
    target.mkdir()
    record_install(_fake_record(install_path=str(target)))
    rc, out = _run_list_cli(
        "--json",
        "--source",
        "axi",
        capsys=capsys,
        monkeypatch=monkeypatch,
        pip_entries=[_PipEntry(name="alpha", version="0.2.0", enabled=True)],
    )
    assert rc == 0
    data = json.loads(out)
    names = [e["name"] for e in data["extensions"]]
    assert "greeter" in names
    assert "alpha" not in names


def test_cli_table_has_columns_header(
    axiom_home: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out = _run_list_cli(
        capsys=capsys,
        monkeypatch=monkeypatch,
        pip_entries=[_PipEntry(name="alpha", version="0.1.0", enabled=True)],
    )
    assert rc == 0
    assert "NAME" in out
    assert "VERSION" in out
    assert "SOURCE" in out
    assert "STATUS" in out
    assert "alpha" in out


# ---------------------------------------------------------------------------
# Legacy no-args path routes through the Provider
# ---------------------------------------------------------------------------


def test_legacy_cmd_list_function_is_retired(axiom_home: Path) -> None:
    """Make sure the legacy ``_cmd_list`` helper is gone."""
    import axiom.extensions.cli as legacy

    assert not hasattr(legacy, "_cmd_list")
