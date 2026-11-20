# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Python library for the chat toolkit.

The toolkit is ``tools``: the registry of callables AXI offers the model.
These names are how other Python calls list and run that registry without
going through the REPL.
"""

from __future__ import annotations

from axiom.extensions.builtins.chat.tools import (
    ToolDef,
    execute_tool,
    get_all_tools,
    get_tool_definitions,
    unknown_tool_message,
)

__all__ = [
    "ToolDef",
    "execute_tool",
    "get_all_tools",
    "get_tool_definitions",
    "unknown_tool_message",
]
