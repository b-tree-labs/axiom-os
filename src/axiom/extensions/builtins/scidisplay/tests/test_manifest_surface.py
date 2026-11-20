# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every capability the manifest declares must exist (a declared surface that does not is a lie).

`axiom_compute__verify` was declared against `compute:verify`, and `compute.py` had no `verify`.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

MANIFEST = Path(__file__).resolve().parents[1] / "axiom-extension.toml"
PROVIDES = tomllib.loads(MANIFEST.read_text())["extension"]["provides"]


@pytest.mark.parametrize(
    "entry", [p["entry"] for p in PROVIDES if "entry" in p], ids=lambda e: e.split(".")[-1]
)
def test_a_declared_entry_resolves_to_a_callable(entry):
    module_name, _, attr = entry.partition(":")
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attr, None)), f"{entry} is declared but does not exist"
