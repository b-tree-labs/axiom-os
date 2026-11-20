# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Interactive conversational agent.

Houses **AXI**, the Loop agent in the platform's REPL cycle. AXI
is the user-facing protagonist — humans talk to AXI, and AXI
dispatches to SCAN / CURIO / PRESS / TIDY / TRIAGE as needed. See
``agents/axi/persona.md`` for AXI's role definition.

Consumer layers rebrand the face per Axiomatic Way principle #7
(a consumer layer may surface him under its own name); the underlying agent identity is
canonical.

The toolkit is ``tools`` (the model-facing registry). ``library`` is the
importable Python API for that registry: ``ToolDef``, ``get_all_tools``,
``execute_tool``, ``get_tool_definitions``, ``unknown_tool_message``.
Those names are also available on this package, resolved on first use so
importing ``chat`` does not build the registry.
"""

__all__ = [
    "ToolDef",
    "execute_tool",
    "get_all_tools",
    "get_tool_definitions",
    "library",
    "unknown_tool_message",
]

_LAZY = frozenset(__all__)


def __getattr__(name: str):
    # importlib, not ``from . import``: the package is already on the stack,
    # and a circular ``from package import name`` calls this function again.
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    library = importlib.import_module("axiom.extensions.builtins.chat.library")
    if name == "library":
        return library
    return getattr(library, name)
