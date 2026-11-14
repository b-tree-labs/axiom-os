# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``doctor`` on a computer ``setup`` just finished says "all set", and means it.

A first-hour release test on 0.70.19: ``setup --invitation`` ended "All set." with every
line green, and the very next ``doctor`` exited 1 with "Found 3 issue(s)":

- ``Package: Not installed`` with ``pip install -e .``: it asked ``pip show``, and a uv-tool
  or pipx environment has no pip. The remedy was a source checkout's.
- ``LLM Gateway: No providers configured``: a client bound to a site answers with the site's
  model, so no local provider is the expected state for it.
- ``Agent Services: not_installed``: background agents are a node's job, optional on a laptop.

Since #1529 ``doctor`` is this install's own rows followed by the setup checklist, the same
checks ``setup`` runs, changing nothing. These tests hold the three shapes the release test
meets (a bound laptop, an unbound laptop, a source checkout) and the node, where the agent
row still counts. Every test runs in a temporary home.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import axiom.axiom_cli as cli
from axiom.extensions.builtins.setup.steps import Line


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / ".axi").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AXI_STATE_DIR", str(home / ".axi"))
    monkeypatch.setenv("AXIOM_NODE_CONFIG", str(home / ".axi" / "node.toml"))
    # The root conftest points AXIOM_DB_URL at a refusing address; a database URL is how a
    # node says it holds the medallion, so a laptop test must not carry one.
    for var in (
        "AXIOM_DB_URL",
        "DATABASE_URL",
        "DP1_RAG_DSN",
        "AXIOM_SAMPLE_DATA",
        "AXIOM_SITE",
        "AXIOM_NODE_URL",
        "AXIOM_VIEW_AS",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture(autouse=True)
def quiet_machine(tmp_path, monkeypatch):
    """Everything doctor asks outside the process, answered by fakes."""
    # An entry point that works, in the interpreter's own bin folder.
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    from axiom.infra.branding import get_branding

    for name in {get_branding().cli_name or "axi", "axi"}:
        (bin_dir / name).write_text("from axiom.axiom_cli import main\nmain()\n")
    monkeypatch.setattr(cli.sys, "executable", str(bin_dir / "python"))

    import axiom.llm.gateway as gateway

    class NoProviders:
        available = False
        active_provider = None

    monkeypatch.setattr(gateway, "Gateway", NoProviders)

    import axiom.extensions.builtins.agents.cli as agents

    daemon = SimpleNamespace(agent=SimpleNamespace(is_always_on=True))
    monkeypatch.setattr(agents, "_discover_agent_extensions", lambda: [daemon])
    monkeypatch.setattr(
        agents,
        "_make_background_service_manager",
        lambda: SimpleNamespace(
            status=lambda: SimpleNamespace(status="not_installed", provider="launchd")
        ),
    )
    monkeypatch.setattr(cli, "_brand_health_extras", lambda: [])
    monkeypatch.setattr(cli, "_llm_diagnose", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_setup_checks", lambda: [ln.as_doctor_row() for ln in GREEN_SETUP])
    monkeypatch.setattr(cli, "_install_method", lambda: "uv tool")
    monkeypatch.setattr(cli, "_find_distribution", lambda name: _dist())


#: What `setup --invitation` ended with on the release test: every line green.
GREEN_SETUP = [
    Line("upgrade", "Up to date", True, "Axiom 0.70.19 is current"),
    Line("vault", "Vault", True, "OpenBao at http://127.0.0.1:8200, unsealed"),
    Line("shell", "Shell", True, "zsh finds the vault (~/.zshrc)"),
    Line("node", "Node", True, "node.example.org answers from this network"),
    Line("key", "Signed in", True, "node.example.org accepts your key as you for site-a"),
    Line("mcp", "AI apps", False, "no AI apps found", skipped=True),
]


def _dist(version="0.70.19", direct_url=None):
    return SimpleNamespace(
        version=version,
        read_text=lambda name: (
            json.dumps(direct_url) if direct_url and name == "direct_url.json" else None
        ),
    )


def _bind(home: Path, site="site-a", face="https://node.example.org") -> None:
    (home / ".axi" / "site-bindings.json").write_text(
        json.dumps({"current": site, "bindings": {site: {"site": site, "face_url": face}}})
    )


def _rows() -> dict[str, dict]:
    return {r["name"]: r for r in cli._gather_diagnostics()["checks"]}


def _issues() -> list[str]:
    return [name for name, r in _rows().items() if not r["ok"]]


# ---------------------------------------------------------------- a bound laptop


def test_a_set_up_bound_laptop_exits_0_with_the_setup_checklist(temp_home, capsys):
    _bind(temp_home)
    assert cli.cmd_doctor() == 0
    out = capsys.readouterr().out
    assert "Setup checklist" in out
    for line in GREEN_SETUP:
        assert line.title in out
    assert "issue" not in out.lower() and "✗" not in out
    assert "All set" in out


def test_a_bound_laptop_counts_the_sites_model_as_its_provider(temp_home):
    _bind(temp_home)
    gw = _rows()["LLM Gateway"]
    assert gw["ok"] and "site-a" in gw["status"] and "node.example.org" in gw["status"]


def test_agent_services_on_a_laptop_are_information_not_an_issue(temp_home):
    _bind(temp_home)
    agents = _rows()["Agent Services"]
    assert agents["ok"] and agents.get("info")
    assert "optional on a laptop" in agents["status"]


def test_a_uv_tool_install_is_an_install(temp_home):
    pkg = _rows()["Package"]
    assert pkg["ok"] and "uv tool" in pkg["status"] and "0.70.19" in pkg["status"]
    assert not pkg.get("fix")


def test_a_pipx_install_is_an_install(temp_home, monkeypatch):
    monkeypatch.setattr(cli, "_install_method", lambda: "pipx")
    pkg = _rows()["Package"]
    assert pkg["ok"] and "pipx" in pkg["status"]


def test_a_pypi_install_outside_a_venv_is_not_told_to_make_one(temp_home, monkeypatch):
    monkeypatch.setattr(cli, "_install_method", lambda: "pip")
    monkeypatch.setattr(cli.sys, "prefix", cli.sys.base_prefix)
    venv = _rows()["Virtual Environment"]
    assert venv["ok"] and venv.get("info") and not venv.get("fix")


def test_the_package_is_found_without_pip(temp_home, monkeypatch):
    """A uv-tool environment has no pip: doctor must not shell out to it."""

    def no_pip(*a, **k):
        raise AssertionError(f"doctor ran a subprocess: {a}")

    monkeypatch.setattr("subprocess.run", no_pip)
    assert _rows()["Package"]["ok"]


# ---------------------------------------------------------------- an unbound laptop


def test_an_unbound_laptop_with_no_provider_is_told_to_configure_one(temp_home, capsys):
    assert _issues() == ["LLM Gateway"]
    gw = _rows()["LLM Gateway"]
    assert "config" in gw["fix"]
    assert cli.cmd_doctor() == 1
    assert "Found 1 issue" in capsys.readouterr().out


def test_an_unbound_laptop_still_treats_agents_as_optional(temp_home):
    agents = _rows()["Agent Services"]
    assert agents["ok"] and agents.get("info")


# ---------------------------------------------------------------- a source checkout


def test_a_source_checkout_that_is_not_installed_is_told_to_install_it_editable(
    temp_home, monkeypatch
):
    monkeypatch.setattr(cli, "_install_method", lambda: "checkout")
    monkeypatch.setattr(cli, "_find_distribution", lambda name: None)
    pkg = _rows()["Package"]
    assert not pkg["ok"] and pkg["fix"] == "pip install -e ."


def test_an_editable_source_checkout_is_installed(temp_home, monkeypatch):
    monkeypatch.setattr(cli, "_install_method", lambda: "checkout")
    monkeypatch.setattr(
        cli,
        "_find_distribution",
        lambda name: _dist(
            direct_url={"url": "file:///work/axiom", "dir_info": {"editable": True}}
        ),
    )
    pkg = _rows()["Package"]
    assert pkg["ok"] and pkg["status"] == "Editable at /work/axiom"


def test_an_installed_package_that_cannot_be_found_gets_its_installers_remedy(
    temp_home, monkeypatch
):
    monkeypatch.setattr(cli, "_find_distribution", lambda name: None)
    pkg = _rows()["Package"]
    assert not pkg["ok"] and pkg["fix"].startswith("uv tool install ")


# ---------------------------------------------------------------- a node


def test_on_a_node_a_stopped_background_service_is_an_issue(temp_home, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://127.0.0.1:1/none")
    assert cli._gather_diagnostics()["shape"] == "node"
    agents = _rows()["Agent Services"]
    assert not agents["ok"] and not agents.get("info") and "agents register" in agents["fix"]


def test_a_node_whose_policy_keeps_agents_off_is_not_told_to_start_them(temp_home):
    (temp_home / ".axi" / "node.toml").write_text(
        '[node]\nrole = "collector"\nfunctions = ["acquire"]\nagents = "none"\n'
    )
    assert cli._gather_diagnostics()["shape"] == "node"
    agents = _rows()["Agent Services"]
    assert agents["ok"] and agents.get("info") and "policy" in agents["status"]


# ---------------------------------------------------------------- the report


def test_a_setup_line_that_does_not_apply_is_shown_with_a_dot_and_not_counted(temp_home):
    rows = [ln.as_doctor_row() for ln in GREEN_SETUP]
    text = cli.render_doctor_report(rows, cli="axi", shape="laptop")
    assert "   · AI apps" in text
    assert all(r["ok"] for r in rows)


def test_a_red_setup_line_carries_its_one_remedy(temp_home):
    red = Line("node", "Node", False, "node.example.org did not answer", "connect your VPN")
    text = cli.render_doctor_report([red.as_doctor_row()], cli="axi")
    assert "✗ Node" in text and "→ connect your VPN" in text
