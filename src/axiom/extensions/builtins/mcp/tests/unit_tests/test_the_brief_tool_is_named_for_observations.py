# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-265 step 1 (D9): the brief tool is ``axiom_observations__brief``.

"Signal" is retired. The old name keeps working for one release, because agents and
harnesses name it, and its description says what replaced it and when it goes, so a model
reading the tool list moves to the new name on its own.
"""

from __future__ import annotations

import asyncio

from axiom.extensions.builtins.mcp.platform_primitives import (
    PLATFORM_TOOL_NAMES,
    PlatformPrimitives,
)

NEW, OLD = "axiom_observations__brief", "axiom_signals__brief"


def _tools():
    return {t.name: t for t in PlatformPrimitives.contribution().tools}


def test_the_new_name_is_a_platform_tool_with_the_same_arguments():
    tools = _tools()
    assert NEW in PLATFORM_TOOL_NAMES and NEW in tools
    assert tools[NEW].input_schema == tools[OLD].input_schema


def test_the_old_name_says_it_is_deprecated_and_names_the_new_one():
    desc = _tools()[OLD].description
    assert desc.startswith("Deprecated:") and NEW in desc and "v0.72.0" in desc
    assert not _tools()[NEW].description.startswith("Deprecated")


def test_both_names_answer_the_same_way(tmp_axiom_home):
    dispatch = PlatformPrimitives.contribution().dispatch
    new, old = asyncio.run(dispatch[NEW]({})), asyncio.run(dispatch[OLD]({}))
    assert isinstance(new, dict) and set(new) == set(old)
