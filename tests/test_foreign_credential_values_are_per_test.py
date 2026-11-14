# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""No two xdist workers share a state directory (#1425).

``ForeignCredentialStore(tmp_path)`` isolates the metadata index only; the
values go to the ``file`` backend at ``<AXI_STATE_DIR>/secrets/foreign-values.json``.
The root conftest chose ``AXI_STATE_DIR`` once, in the xdist controller, and
every worker inherited it, so every test on every worker read and rewrote one
values file. Two workers writing it at once lost one side's write, and a test
then read ``KeyError`` for a credential it had stored a line earlier: the
secrets and vault suites failed about one run in twelve, a different test each
time, and passed alone.

The values file must still follow ``AXI_STATE_DIR``: a test that points a site
at a state directory and stores a key there with the ``secrets set`` CLI (a
subprocess, which reads the default path) expects its in-process reader to see
the same file. A per-test ``AXIOM_FILE_SECRETS_PATH`` broke exactly that.
"""

from __future__ import annotations

import os
from pathlib import Path


def _values_path() -> str:
    from axiom.extensions.builtins.secrets import _default_config_for_scheme

    return _default_config_for_scheme("file")["path"]


def test_a_worker_has_a_state_dir_of_its_own():
    controller = os.environ.get("AXI_TEST_CONTROLLER_STATE_DIR", "")
    if os.environ.get("PYTEST_XDIST_WORKER") and controller:
        # Only a dir the controller made for itself is replaced; a chosen one is kept.
        assert os.environ["AXI_STATE_DIR"] != controller


def test_the_values_file_follows_the_state_dir_a_test_sets(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "site"))
    assert Path(_values_path()) == tmp_path / "site" / "secrets" / "foreign-values.json"
