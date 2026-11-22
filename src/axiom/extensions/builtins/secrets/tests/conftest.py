# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Secrets tests never read the vault of the machine they run on.

The providers fall back to state under the home directory (~/.axi/openbao,
the keyring), so on a developer's machine with a local vault set up three
tests found a real backend where they asserted there was none, and failed
(2026-10-29). Every secrets test gets an empty home; a test that wants a
backend creates it there.
"""

import pytest


@pytest.fixture(autouse=True)
def _empty_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    return home
