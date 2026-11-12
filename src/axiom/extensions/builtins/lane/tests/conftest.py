# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Lane tests never read the machine they run on.

The doctor also checks ~/.axi/service-venv, so on a developer's machine whose
service venv is an editable install it found a real breakage and failed a test
about something else entirely (2026-10-28). Every lane test gets an empty home.
"""

import pytest


@pytest.fixture(autouse=True)
def _empty_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home
