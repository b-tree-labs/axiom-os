# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Test config for the classroom extension.

``axiom-tests`` is a ``pytest11`` plugin — installing it in the active
environment is sufficient for its fixtures to be available here. This
file exists so contributors have an obvious place to add local
fixtures as the suite grows.
"""

import pytest


@pytest.fixture(autouse=True)
def _state_follows_home(tmp_path, monkeypatch):
    """Each simulated person's state lives under their own HOME (#2010).

    These tests play an instructor and several students on one machine by
    switching ``HOME``. State resolves through ``get_user_state_dir()``, which
    follows ``AXI_STATE_DIR`` first, and the root conftest sets that for the
    whole session; left set, every person here would share one folder. So it
    is removed, and ``HOME`` starts at a throwaway folder: a test that does not
    switch it still never reaches a real home folder.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AXI_STATE_DIR", raising=False)
