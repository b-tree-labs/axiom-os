# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The node's own memory store retires reference fragments whose record lost its content.

A log removes a record's content under its retention rule with a signed tombstone (unit LD-6);
:func:`axiom.memory.references.retire` then asks every registered store to take the fragments
that point at the record out of recall. This registers the node's default store (the one the
memory MCP server and the ``/memory`` endpoint serve), opened the same way they open it. Recall
of a fragment any store still holds resolves to the tombstone regardless (LD-3-A6).
"""

from __future__ import annotations

from typing import Any

from axiom.memory import references

NAME = "user-memory"


def retire_user_references(pointers: list[str], tombstone: dict[str, Any]) -> list[str]:
    from .mcp_server import _build_default_composition

    return _build_default_composition().retire_references(pointers, tombstone=tombstone)


references.register_retirer(NAME, retire_user_references)

__all__ = ["NAME", "retire_user_references"]
