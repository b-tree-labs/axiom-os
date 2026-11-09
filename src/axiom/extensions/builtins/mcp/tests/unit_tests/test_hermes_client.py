# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Hermes Agent as a first-class MCP client, with its EC determination.

Hermes (Nous Research) keeps MCP servers in ``~/.hermes/config.yaml`` under
``mcp_servers``, not in a JSON file, so it needs its own writer alongside
``mcp_json`` / ``vscode_json`` / ``codex_toml``.

The EC determination is the load-bearing part. Hermes is provider-agnostic
and *could* in principle be pointed at the local ingress, but its data path
has not been verified here — and the registry's stated rule is fail-closed:
clients not yet verified are ``model_routable=False``. Being generous with
that flag is how export-controlled tool output reaches a public cloud, so
Hermes lands non-routable with an UNVERIFIED note, exactly as windsurf and
gemini did.

Its ``tools.include`` list is also preserved on write. That list is what
keeps a third-party agent away from the KEEP credential tools; silently
dropping it on a re-install would be a privilege escalation delivered by
an upgrade.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


def _spec():
    from axiom.extensions.builtins.mcp.install import _SPEC_BY_NAME

    return _SPEC_BY_NAME["hermes"]


# ---------------------------------------------------------------------------
# Registry membership + EC determination
# ---------------------------------------------------------------------------


def test_hermes_is_a_supported_client():
    from axiom.extensions.builtins.mcp.install import supported_tools

    assert "hermes" in supported_tools()


def test_hermes_is_fail_closed_non_ec_until_verified():
    """Unverified data path means non-routable. Never guess generously."""
    spec = _spec()
    assert spec.model_routable is False
    assert "UNVERIFIED" in spec.ec_notes


def test_hermes_appears_in_the_capability_chart():
    from axiom.extensions.builtins.mcp.install import client_capabilities

    row = next(r for r in client_capabilities() if r["client"] == "hermes")
    assert row["ec_routable"] is False


def test_ec_capable_is_false_for_hermes_even_when_routed():
    """model_routable=False must dominate --route-model."""
    from axiom.extensions.builtins.mcp.install import _ec_capable

    assert _ec_capable(_spec(), True) is False
    assert _ec_capable(_spec(), False) is False


# ---------------------------------------------------------------------------
# YAML writer
# ---------------------------------------------------------------------------


def test_install_writes_mcp_servers_yaml(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.mcp.install import _install_one

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"_config_version": 3, "mcp_servers": {}}))
    monkeypatch.setenv("AXIOM_HERMES_CONFIG", str(cfg))

    action = _install_one(
        _spec(), "axiom", "/pinned/python", ["-m", "axiom.x"], {"A": "1"},
        dry_run=False,
    )

    data = yaml.safe_load(cfg.read_text())
    assert action in ("added", "updated")
    entry = data["mcp_servers"]["axiom"]
    assert entry["command"] == "/pinned/python"
    assert entry["args"] == ["-m", "axiom.x"]
    assert entry["env"] == {"A": "1"}
    # Unrelated top-level config survives.
    assert data["_config_version"] == 3


def test_install_preserves_other_servers(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.mcp.install import _install_one

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"mcp_servers": {
        "filesystem": {"command": "npx", "args": ["-y", "server-filesystem"]},
    }}))
    monkeypatch.setenv("AXIOM_HERMES_CONFIG", str(cfg))

    _install_one(_spec(), "axiom", "/pinned/python", ["-m", "x"], {}, dry_run=False)

    data = yaml.safe_load(cfg.read_text())
    assert data["mcp_servers"]["filesystem"]["command"] == "npx"
    assert "axiom" in data["mcp_servers"]


def test_reinstall_preserves_the_tools_include_allowlist(
    tmp_path: Path, monkeypatch,
):
    """The allowlist is a security control, not a preference.

    ``tools.include`` is what keeps a third-party agent away from the KEEP
    credential tools. Dropping it on re-install would hand those back
    silently.
    """
    from axiom.extensions.builtins.mcp.install import _install_one

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"mcp_servers": {"axiom": {
        "command": "/old/python",
        "args": ["-m", "axiom.x"],
        "tools": {"include": ["axiom_memory_recall", "axiom_memory_append"]},
    }}}))
    monkeypatch.setenv("AXIOM_HERMES_CONFIG", str(cfg))

    _install_one(_spec(), "axiom", "/pinned/python", ["-m", "axiom.x"], {},
                 dry_run=False)

    entry = yaml.safe_load(cfg.read_text())["mcp_servers"]["axiom"]
    assert entry["command"] == "/pinned/python"          # repointed
    assert entry["tools"]["include"] == [                 # but allowlist kept
        "axiom_memory_recall", "axiom_memory_append",
    ]


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.mcp.install import _install_one

    cfg = tmp_path / "config.yaml"
    original = yaml.safe_dump({"mcp_servers": {}})
    cfg.write_text(original)
    monkeypatch.setenv("AXIOM_HERMES_CONFIG", str(cfg))

    _install_one(_spec(), "axiom", "/pinned/python", ["-m", "x"], {}, dry_run=True)
    assert cfg.read_text() == original


def test_uninstall_removes_only_our_entry(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.mcp.install import _uninstall_one

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"mcp_servers": {
        "axiom": {"command": "/pinned/python"},
        "filesystem": {"command": "npx"},
    }}))
    monkeypatch.setenv("AXIOM_HERMES_CONFIG", str(cfg))

    _uninstall_one(_spec(), dry_run=False)

    servers = yaml.safe_load(cfg.read_text())["mcp_servers"]
    assert "axiom" not in servers
    assert "filesystem" in servers


def test_missing_config_is_created(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.mcp.install import _install_one

    cfg = tmp_path / "nested" / "config.yaml"
    monkeypatch.setenv("AXIOM_HERMES_CONFIG", str(cfg))

    _install_one(_spec(), "axiom", "/pinned/python", ["-m", "x"], {}, dry_run=False)

    assert yaml.safe_load(cfg.read_text())["mcp_servers"]["axiom"]["command"] == (
        "/pinned/python"
    )


def test_reinstall_with_identical_entry_is_unchanged(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.mcp.install import _install_one

    cfg = tmp_path / "config.yaml"
    monkeypatch.setenv("AXIOM_HERMES_CONFIG", str(cfg))

    assert _install_one(_spec(), "axiom", "/p", ["-m", "x"], {"A": "1"}, dry_run=False) == "added"
    body = cfg.read_text()
    assert _install_one(_spec(), "axiom", "/p", ["-m", "x"], {"A": "1"}, dry_run=False) == "unchanged"
    assert cfg.read_text() == body
    assert _install_one(_spec(), "axiom", "/q", ["-m", "x"], {"A": "1"}, dry_run=False) == "updated"


def test_malformed_yaml_is_refused_not_rewritten(tmp_path: Path, monkeypatch):
    """A harness config is the user's; never clobber what we can't parse."""
    from axiom.extensions.builtins.mcp.install import _install_one, read_entry

    cfg = tmp_path / "config.yaml"
    cfg.write_text("{ [ } ]\n")
    monkeypatch.setenv("AXIOM_HERMES_CONFIG", str(cfg))

    with pytest.raises(ValueError):
        _install_one(_spec(), "axiom", "/p", ["-m", "x"], {}, dry_run=False)
    assert cfg.read_text() == "{ [ } ]\n"
    with pytest.raises(ValueError):
        read_entry(_spec(), "axiom")


def test_non_mapping_yaml_is_refused(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.mcp.install import _install_one

    cfg = tmp_path / "config.yaml"
    cfg.write_text("- just\n- a list\n")
    monkeypatch.setenv("AXIOM_HERMES_CONFIG", str(cfg))

    with pytest.raises(ValueError):
        _install_one(_spec(), "axiom", "/p", ["-m", "x"], {}, dry_run=False)
    assert cfg.read_text() == "- just\n- a list\n"
