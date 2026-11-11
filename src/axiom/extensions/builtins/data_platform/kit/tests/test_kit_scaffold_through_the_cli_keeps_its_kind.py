# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`axi data kit-scaffold <kind> <name>` reaches the skill with its kind.

The CLI dropped every argument named `kind` before calling a skill (only
`register` put it back), so `kit-scaffold conform x` arrived as no kind at all
and every scaffold from the terminal failed with "kind '' is not one of ...".
The skill tests called the skill directly and never saw it.
"""

from __future__ import annotations

import argparse

import pytest

from axiom.extensions.builtins.data_platform import cli


def _parse(argv: list[str]) -> argparse.Namespace:
    return cli._parser().parse_args(argv)


@pytest.mark.parametrize("kind", ["conform", "derived", "gold", "verb", "chart"])
def test_the_kind_reaches_the_skill(kind):
    args = _parse(["kit-scaffold", kind, "example_thing"])
    params = cli._args_to_params(args)
    assert params.get("kind") == kind and params.get("name") == "example_thing"


def test_register_still_handles_its_own_kind():
    args = _parse(["kit-scaffold", "gold", "x"])
    assert "verb" not in cli._args_to_params(args)
