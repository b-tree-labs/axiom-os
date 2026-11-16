# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An MCP server an app starts reads with the key the product's command stored.

A product built on Axiom (``acme`` here) files the Keys page's key under ``~/.acme``
when its own command stores it (``acme vault set node-key``, ``acme site redeem``);
the binding that names the node is machine state under ``~/.axi``. The MCP server an
app starts runs as no product, so it looked only in ``~/.axi`` and ``mcp doctor`` said
``FAIL Binding key: no token_ref and no stored key``: every bound read from the app
would have been refused while the same read worked in a terminal. Found by running a
product's onboarding guide as written, against Axiom 0.70.17.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.data_platform import read_target as rt

FACE = "http://node.example.org"


class FakeStore:
    """A credential store whose folder holds ``node-key`` when a marker file says so."""

    def __init__(self, state_dir):
        self.dir = Path(state_dir)

    def exists(self, name):
        return (self.dir / f"holds-{name}").exists()

    @contextmanager
    def get(self, name):
        if not self.exists(name):
            raise KeyError(name)
        yield SimpleNamespace(as_str=lambda: (self.dir / f"holds-{name}").read_text())


@pytest.fixture
def home(tmp_path, monkeypatch):
    from axiom.extensions.builtins.secrets.foreign import store
    from axiom.infra import branding, paths

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AXI_STATE_DIR", raising=False)
    for name in ("axi", "acme"):
        (tmp_path / f".{name}").mkdir()
    # This process runs as no product: its own folder is ~/.axi.
    monkeypatch.setattr(paths, "get_user_state_dir", lambda: tmp_path / ".axi")
    monkeypatch.setattr(store, "ForeignCredentialStore", FakeStore)
    monkeypatch.setattr(
        branding,
        "discover_portfolio_members",
        lambda: [
            SimpleNamespace(cli_name="axi"),
            SimpleNamespace(cli_name="acme"),
        ],
    )
    monkeypatch.setattr(rt, "_binding_token_ref", lambda: "")
    return tmp_path


def _file_key(folder: Path, value: str) -> None:
    (folder / "holds-node-key").write_text(value)


def test_a_key_the_product_command_filed_is_found_by_a_server_running_as_no_product(home):
    _file_key(home / ".acme", "key-stored-by-acme")
    assert rt._key(FACE) == "key-stored-by-acme"


def test_this_processs_own_folder_is_read_first(home):
    _file_key(home / ".axi", "own")
    _file_key(home / ".acme", "other")
    assert rt._key(FACE) == "own"


def test_an_explicit_state_folder_is_the_whole_answer(home, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(home / ".axi"))
    _file_key(home / ".acme", "key-stored-by-acme")
    assert rt._key(FACE) == ""


def test_no_key_anywhere_is_an_empty_answer_not_an_error(home):
    assert rt._key(FACE) == ""


def test_the_key_is_still_offered_only_to_the_bound_node(home, monkeypatch):
    _file_key(home / ".acme", "key-stored-by-acme")
    monkeypatch.setattr(rt, "current_binding", lambda: ("example-loop", FACE))
    assert rt.bound_key_for(FACE + "/data/query") == "key-stored-by-acme"
    assert rt.bound_key_for("http://elsewhere.example.org/data/query") == ""
