# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext search`` — substring match over registry metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from axiom.cli.ext.commands.search import (
    SearchHit,
    SearchProvider,
    search_registry,
)
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.registry_backend import put


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    monkeypatch.delenv("AXIOM_REGISTRY_URL", raising=False)
    return home


def _seed(
    tmp_path: Path,
    name: str,
    version: str,
    description: str = "",
    tags: tuple[str, ...] = (),
) -> None:
    stage = tmp_path / f"stage-{name}-{version}"
    stage.mkdir()
    manifest = stage / "axiom-extension.toml"
    tag_line = ""
    if tags:
        tag_line = "tags = [" + ", ".join(f'"{t}"' for t in tags) + "]\n"
    manifest.write_text(
        "[extension]\n"
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        f'description = "{description}"\n'
        + tag_line,
        encoding="utf-8",
    )
    artifact = stage / f"{name}-{version}.tar.gz"
    artifact.write_bytes(b"stub")
    sig = stage / f"{name}-{version}.tar.gz.sig"
    sig.write_text("deadbeef", encoding="utf-8")
    put(
        name,
        version,
        manifest,
        artifact,
        sig,
        {"publisher": "b-tree-labs", "published_at": "2026-04-22T00:00:00Z"},
    )


def _run_search_cli(
    *argv: str, capsys
) -> tuple[int, str, str]:
    provider = SearchProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(list(argv))
    ctx = CliContext(cwd=Path.cwd())
    capsys.readouterr()
    rc = provider.run(args, ctx)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_search_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "search" in providers
    assert providers["search"].verb == "search"


# ---------------------------------------------------------------------------
# Direct function (happy paths)
# ---------------------------------------------------------------------------


def test_search_matches_name(axiom_home: Path, tmp_path: Path) -> None:
    _seed(tmp_path, "greeter", "0.1.0", "friendly hello")
    _seed(tmp_path, "other", "0.1.0", "unrelated tool")
    hits = search_registry("greet")
    assert [h.name for h in hits] == ["greeter"]


def test_search_matches_description(
    axiom_home: Path, tmp_path: Path
) -> None:
    _seed(tmp_path, "foo", "0.1.0", "sends friendly hello to everybody")
    _seed(tmp_path, "bar", "0.1.0", "unrelated")
    hits = search_registry("hello")
    assert [h.name for h in hits] == ["foo"]


def test_search_matches_tag(axiom_home: Path, tmp_path: Path) -> None:
    _seed(tmp_path, "foo", "0.1.0", "x", tags=("greeting", "demo"))
    _seed(tmp_path, "bar", "0.1.0", "y", tags=("storage",))
    hits = search_registry("storage")
    assert [h.name for h in hits] == ["bar"]


def test_search_is_case_insensitive(
    axiom_home: Path, tmp_path: Path
) -> None:
    _seed(tmp_path, "foo", "0.1.0", "Says HELLO politely")
    hits = search_registry("HeLlO")
    assert [h.name for h in hits] == ["foo"]


def test_search_no_matches_returns_empty(
    axiom_home: Path, tmp_path: Path
) -> None:
    _seed(tmp_path, "foo", "0.1.0", "hi")
    hits = search_registry("nothing-like-this")
    assert hits == []


def test_search_hit_carries_latest_and_description(
    axiom_home: Path, tmp_path: Path
) -> None:
    _seed(tmp_path, "foo", "0.1.0", "old")
    _seed(tmp_path, "foo", "0.2.0", "newer description")
    hits = search_registry("foo")
    assert len(hits) == 1
    assert hits[0].latest == "0.2.0"
    assert hits[0].description == "newer description"


# ---------------------------------------------------------------------------
# CLI wrapper
# ---------------------------------------------------------------------------


def test_cli_table_output(
    axiom_home: Path, tmp_path: Path, capsys
) -> None:
    _seed(tmp_path, "greeter", "0.1.0", "says hello")
    rc, out, _ = _run_search_cli("greet", capsys=capsys)
    assert rc == 0
    assert "greeter" in out
    assert "NAME" in out and "LATEST" in out
    assert "says hello" in out


def test_cli_no_matches_exits_zero_with_stderr(
    axiom_home: Path, tmp_path: Path, capsys
) -> None:
    _seed(tmp_path, "foo", "0.1.0", "hi")
    rc, out, err = _run_search_cli("nothing", capsys=capsys)
    assert rc == 0
    assert out == ""  # no table on empty result
    assert "no extensions match 'nothing'" in err


def test_cli_json_output(axiom_home: Path, tmp_path: Path, capsys) -> None:
    _seed(tmp_path, "greeter", "0.1.0", "says hello", tags=("friendly",))
    rc, out, _ = _run_search_cli("greet", "--json", capsys=capsys)
    assert rc == 0
    data = json.loads(out)
    assert data["query"] == "greet"
    assert len(data["hits"]) == 1
    hit = data["hits"][0]
    assert hit["name"] == "greeter"
    assert hit["latest"] == "0.1.0"
    assert hit["description"] == "says hello"
    assert hit["tags"] == ["friendly"]


def test_cli_registry_override_file_scheme(
    axiom_home: Path, tmp_path: Path, capsys
) -> None:
    alt = tmp_path / "alt-registry"
    alt.mkdir()
    # Seed the alt registry via the override env.
    import os as _os

    _os.environ["AXIOM_REGISTRY_URL"] = f"file://{alt}"
    _seed(tmp_path, "alt_ext", "0.1.0", "alternate")
    del _os.environ["AXIOM_REGISTRY_URL"]

    # Default search does not see alt_ext.
    assert [h.name for h in search_registry("alt")] == []

    # Override brings it into view.
    rc, out, _ = _run_search_cli(
        "alt", "--registry", f"file://{alt}", capsys=capsys
    )
    assert rc == 0
    assert "alt_ext" in out


def test_cli_registry_rejects_non_file_scheme(
    axiom_home: Path, capsys
) -> None:
    rc, out, _ = _run_search_cli(
        "foo", "--registry", "https://example.com/registry", capsys=capsys
    )
    assert rc == 1
    assert "file://" in out or "scheme" in out.lower()


def test_cli_empty_query_errors(
    axiom_home: Path, capsys
) -> None:
    rc, out, _ = _run_search_cli("   ", capsys=capsys)
    assert rc == 2
    assert "empty" in out.lower()


def test_search_hit_dataclass_to_json_shape() -> None:
    hit = SearchHit(name="x", latest="1.0", description="d", tags=("t",))
    assert hit.to_json() == {
        "name": "x",
        "latest": "1.0",
        "description": "d",
        "tags": ["t"],
    }
