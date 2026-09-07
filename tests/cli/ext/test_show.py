# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext show`` — detailed metadata for a registry artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from axiom.cli.ext.commands.publish import publish_extension
from axiom.cli.ext.commands.show import (
    ShowProvider,
    ShowView,
    build_installed_view,
    build_registry_view,
    parse_spec,
)
from axiom.cli.ext.install_state import InstallRecord, record_install
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
    *,
    description: str = "",
    owner: str = "b-tree-labs",
    license: str = "Apache-2.0",
    provides: tuple[tuple[str, str], ...] = (("cmd", "noun"),),
    compatibility: dict[str, str] | None = None,
    depends_on: tuple[str, ...] = (),
    attestation: dict | None = None,
) -> None:
    stage = tmp_path / f"stage-{name}-{version}"
    stage.mkdir()
    manifest = stage / "axiom-extension.toml"
    lines = [
        "[extension]",
        f'name = "{name}"',
        f'version = "{version}"',
        f'description = "{description}"',
        f'owner = "{owner}"',
        f'license = "{license}"',
    ]
    if depends_on:
        deps = ", ".join(f'"{d}"' for d in depends_on)
        lines.append(f"depends_on = [{deps}]")
    if compatibility:
        lines.append("[extension.compatibility]")
        for k, v in compatibility.items():
            lines.append(f'{k} = "{v}"')
    for kind, noun in provides:
        lines.append("[[extension.provides]]")
        lines.append(f'kind = "{kind}"')
        lines.append(f'noun = "{noun}"')
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    artifact = stage / f"{name}-{version}.tar.gz"
    artifact.write_bytes(b"stub-payload")
    sig = stage / f"{name}-{version}.tar.gz.sig"
    sig.write_text("deadbeef", encoding="utf-8")

    att = attestation or {
        "publisher": owner,
        "published_at": "2026-04-22T00:00:00Z",
        "artifact_sha256": "abc123",
        "sig_algo": "ed25519",
    }
    put(name, version, manifest, artifact, sig, att)


def _run_show_cli(
    *argv: str, capsys
) -> tuple[int, str]:
    provider = ShowProvider()
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


def test_show_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "show" in providers
    assert providers["show"].verb == "show"


# ---------------------------------------------------------------------------
# parse_spec
# ---------------------------------------------------------------------------


def test_parse_spec_plain_name() -> None:
    assert parse_spec("greeter") == ("greeter", None)


def test_parse_spec_with_version() -> None:
    assert parse_spec("greeter@0.1.0") == ("greeter", "0.1.0")


def test_parse_spec_rejects_empty_parts() -> None:
    with pytest.raises(ValueError):
        parse_spec("@0.1.0")
    with pytest.raises(ValueError):
        parse_spec("greeter@")


# ---------------------------------------------------------------------------
# Registry view
# ---------------------------------------------------------------------------


def test_build_registry_view_happy_path(
    axiom_home: Path, tmp_path: Path
) -> None:
    _seed(
        tmp_path,
        "greeter",
        "0.1.0",
        description="friendly hello",
        provides=(("cmd", "greet"), ("tool", "shout")),
        compatibility={"python": ">=3.11", "axiom": ">=0.10"},
        depends_on=("other_ext",),
    )
    view = build_registry_view("greeter", None)
    assert view is not None
    assert view.name == "greeter"
    assert view.version == "0.1.0"
    assert view.description == "friendly hello"
    assert view.owner == "b-tree-labs"
    assert view.license == "Apache-2.0"
    assert view.capabilities == ["cmd:greet", "tool:shout"]
    assert view.compatibility == {"python": ">=3.11", "axiom": ">=0.10"}
    assert view.depends_on == ["other_ext"]
    assert view.publisher == "b-tree-labs"
    assert view.published_at == "2026-04-22T00:00:00Z"
    assert view.signature_sha256  # computed from the on-disk .sig
    assert view.source == "registry"


def test_build_registry_view_picks_latest_when_version_omitted(
    axiom_home: Path, tmp_path: Path
) -> None:
    _seed(tmp_path, "greeter", "0.1.0", description="old")
    _seed(tmp_path, "greeter", "0.2.0", description="newer")
    view = build_registry_view("greeter", None)
    assert view is not None
    assert view.version == "0.2.0"
    assert view.description == "newer"


def test_build_registry_view_pinned_version(
    axiom_home: Path, tmp_path: Path
) -> None:
    _seed(tmp_path, "greeter", "0.1.0", description="old")
    _seed(tmp_path, "greeter", "0.2.0", description="newer")
    view = build_registry_view("greeter", "0.1.0")
    assert view is not None
    assert view.version == "0.1.0"
    assert view.description == "old"


def test_build_registry_view_missing_returns_none(
    axiom_home: Path,
) -> None:
    assert build_registry_view("nope", None) is None


def test_signature_status_is_key_unknown_when_no_trusted_key(
    axiom_home: Path, tmp_path: Path
) -> None:
    _seed(
        tmp_path,
        "greeter",
        "0.1.0",
        attestation={
            "publisher": "x",
            "published_at": "t",
            "artifact_sha256": "abc",
            "public_key_sha256": "never-heard-of-this",
        },
    )
    view = build_registry_view("greeter", None)
    assert view is not None
    assert view.signature_status == "key unknown"


def test_signature_status_verified_after_real_publish(
    scaffolded_extension, axiom_home: Path
) -> None:
    """End-to-end: publish through sign produces a signature show can verify."""
    ext = scaffolded_extension("verified_ext")
    publish_extension(ext, yes=True, skip_tag_check=True)
    view = build_registry_view("verified_ext", None)
    assert view is not None
    assert view.signature_status == "verified"


def test_installed_badge_when_state_has_matching_version(
    axiom_home: Path, tmp_path: Path
) -> None:
    _seed(tmp_path, "greeter", "0.1.0", description="d")
    rec = InstallRecord(
        name="greeter",
        version="0.1.0",
        installed_at="2026-04-22T12:00:00Z",
        install_path=str(tmp_path / "x"),
        artifact_sha256="a",
        signature_sha256="b",
        registry_url="file:///x",
    )
    record_install(rec)
    view = build_registry_view("greeter", None)
    assert view is not None
    assert view.installed is True


# ---------------------------------------------------------------------------
# Installed view
# ---------------------------------------------------------------------------


def test_build_installed_view_reads_state(axiom_home: Path) -> None:
    record_install(
        InstallRecord(
            name="greeter",
            version="0.1.0",
            installed_at="2026-04-22T12:00:00Z",
            install_path="/tmp/fake",
            artifact_sha256="art",
            signature_sha256="sig",
            registry_url="file:///x",
        )
    )
    view = build_installed_view("greeter")
    assert view is not None
    assert view.installed is True
    assert view.version == "0.1.0"
    assert view.artifact_sha256 == "art"
    assert view.signature_sha256 == "sig"
    assert view.source == "installed"


def test_build_installed_view_missing_returns_none(axiom_home: Path) -> None:
    assert build_installed_view("nope") is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_happy_path_prints_fields(
    axiom_home: Path, tmp_path: Path, capsys
) -> None:
    _seed(tmp_path, "greeter", "0.1.0", description="friendly")
    rc, out = _run_show_cli("greeter", capsys=capsys)
    assert rc == 0
    assert "greeter 0.1.0" in out
    assert "friendly" in out
    assert "b-tree-labs" in out
    assert "cmd:noun" in out


def test_cli_version_pin_via_at_syntax(
    axiom_home: Path, tmp_path: Path, capsys
) -> None:
    _seed(tmp_path, "greeter", "0.1.0", description="old")
    _seed(tmp_path, "greeter", "0.2.0", description="newer")
    rc, out = _run_show_cli("greeter@0.1.0", capsys=capsys)
    assert rc == 0
    assert "old" in out
    assert "0.1.0" in out


def test_cli_version_pin_via_flag(
    axiom_home: Path, tmp_path: Path, capsys
) -> None:
    _seed(tmp_path, "greeter", "0.1.0", description="old")
    _seed(tmp_path, "greeter", "0.2.0", description="newer")
    rc, out = _run_show_cli(
        "greeter", "--version", "0.1.0", capsys=capsys
    )
    assert rc == 0
    assert "old" in out


def test_cli_missing_extension_exits_one(
    axiom_home: Path, capsys
) -> None:
    rc, out = _run_show_cli("nothing", capsys=capsys)
    assert rc == 1
    assert "axi ext search" in out


def test_cli_json_shape(
    axiom_home: Path, tmp_path: Path, capsys
) -> None:
    _seed(
        tmp_path,
        "greeter",
        "0.1.0",
        description="friendly",
        provides=(("cmd", "greet"),),
    )
    rc, out = _run_show_cli("greeter", "--json", capsys=capsys)
    assert rc == 0
    data = json.loads(out)
    assert data["name"] == "greeter"
    assert data["version"] == "0.1.0"
    assert "cmd:greet" in data["capabilities"]
    assert "signature_status" in data


def test_cli_installed_flag_reads_state(
    axiom_home: Path, capsys
) -> None:
    record_install(
        InstallRecord(
            name="greeter",
            version="0.1.0",
            installed_at="2026-04-22T12:00:00Z",
            install_path="/tmp/fake",
            artifact_sha256="art",
            signature_sha256="sig",
            registry_url="file:///x",
        )
    )
    rc, out = _run_show_cli("greeter", "--installed", capsys=capsys)
    assert rc == 0
    assert "greeter 0.1.0" in out
    assert "(installed)" in out


def test_cli_installed_flag_missing_exits_one(
    axiom_home: Path, capsys
) -> None:
    rc, out = _run_show_cli(
        "nope", "--installed", capsys=capsys
    )
    assert rc == 1
    assert "axi ext list" in out


def test_cli_registry_rejects_non_file_scheme(
    axiom_home: Path, capsys
) -> None:
    rc, out = _run_show_cli(
        "greeter", "--registry", "https://example.com/reg", capsys=capsys
    )
    assert rc == 1
    assert "file://" in out or "scheme" in out.lower()


def test_showview_to_json_roundtrips() -> None:
    view = ShowView(name="x", version="1.0", installed=False)
    data = view.to_json()
    assert data["name"] == "x"
    assert data["version"] == "1.0"
