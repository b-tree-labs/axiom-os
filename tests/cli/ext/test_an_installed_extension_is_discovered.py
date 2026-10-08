# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``ext install`` produces an extension discovery finds, and never hides a working copy (#1160).

Install unpacked under ``$AXIOM_HOME/extensions/<name>-<version>/<name>-<version>/``
while discovery scans ``<state-dir>/extensions/<name>/``: two roots and one
level of nesting apart, so an installed extension was reported installed and
never seen. Install also pip-installed over a creator's editable working copy,
so their later edits silently did nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from axiom.cli.ext.commands import install as install_mod
from axiom.cli.ext.commands.install import install_extension
from axiom.cli.ext.commands.publish import publish_extension
from axiom.cli.ext.commands.uninstall import uninstall_extension
from axiom.extensions.discovery import discover_extensions


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("AXIOM_HOME", str(home))
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.delenv("AXIOM_REGISTRY_URL", raising=False)
    monkeypatch.setenv("AXIOM_INSTALL_NO_PIP", "1")
    return state


def _publish(scaffolded_extension, name: str = "greeter") -> Path:
    ext = scaffolded_extension(name)
    publish_extension(ext, yes=True, skip_tag_check=True)
    return ext


def test_an_installed_extension_is_discovered(homes, scaffolded_extension):
    _publish(scaffolded_extension)
    install_extension("greeter")
    names = {e.name for e in discover_extensions(homes / "extensions")}
    assert "greeter" in names


def test_a_working_copy_already_linked_is_kept_and_named(homes, scaffolded_extension):
    ext = _publish(scaffolded_extension)
    (homes / "extensions").mkdir(parents=True)
    (homes / "extensions" / "greeter").symlink_to(ext)  # the creator's own link
    said: list[str] = []
    install_extension("greeter", announce=said.append)
    assert (homes / "extensions" / "greeter").resolve() == ext.resolve()
    assert any("working copy" in s and str(ext) in s for s in said), said


def test_uninstall_removes_the_discovery_link(homes, scaffolded_extension):
    _publish(scaffolded_extension)
    install_extension("greeter")
    uninstall_extension("greeter")
    assert not (homes / "extensions" / "greeter").exists()


def test_an_editable_install_is_found_so_pip_does_not_replace_it(tmp_path, monkeypatch):
    site = tmp_path / "site"
    info = site / "greeter-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: greeter\nVersion: 0.1.0\n")
    work = tmp_path / "work" / "greeter"
    work.mkdir(parents=True)
    (info / "direct_url.json").write_text(
        json.dumps({"url": work.as_uri(), "dir_info": {"editable": True}})
    )
    monkeypatch.setattr(sys, "path", [str(site), *sys.path])
    assert install_mod._editable_install_of("greeter") == str(work)
    assert install_mod._editable_install_of("not-installed-anywhere") is None
