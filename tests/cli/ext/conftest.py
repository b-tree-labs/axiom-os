# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for ``axi ext`` CLI tests.

The primary fixture here is :func:`scaffolded_extension`, which produces a
freshly initialized AEOS-conformant extension inside a pytest tmp_path. It
uses :class:`axiom.cli.ext.commands.init.InitProvider` so the layout stays
in sync with the real scaffold without duplicating its logic in tests.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from axiom.cli.ext.commands.init import InitProvider
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def scaffolded_extension(tmp_path: Path):
    """Return a factory that scaffolds a fresh extension under tmp_path.

    Usage in tests::

        def test_something(scaffolded_extension):
            ext_path = scaffolded_extension("my_ext")
            # ... exercise a verb against ext_path
    """

    def _scaffold(name: str = "sample_ext") -> Path:
        provider = InitProvider()
        parser = argparse.ArgumentParser()
        provider.add_arguments(parser)
        args = parser.parse_args([name, "--dir", str(tmp_path)])
        ctx = CliContext(cwd=tmp_path)
        rc = provider.run(args, ctx)
        assert rc == 0, f"scaffold failed: rc={rc}"
        return tmp_path / name

    return _scaffold
