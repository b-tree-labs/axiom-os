# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Memory belongs in the one aggregated MCP surface, like every other extension.

Two servers were being registered by every client: the root, which aggregates
each extension's declared tools, and a standalone memory server. They shared no
tool names — the root carried `compose`/`retrieve`/`list` from hardcoded
platform primitives, the standalone carried `append`/`show`/`recent`/`search`/
`recall` plus action provenance plus three credential tools. Two vocabularies
over one ledger, and only the standalone one exposed secrets tooling, which is
why a third-party agent needed a hand-maintained allowlist to be safe.

The manifest called the split transitional and said migrating "requires
extending the AEOS schema". It does not. That would be true for a canonical
`mcp_server` capability kind, but declaring the tools individually — what the
telemetry extension already does — works with the schema as it stands. The
blocker was imagined, which is why this sat unmigrated.

Credential tools are deliberately not carried over. They are reachable through
the CLI and the standalone server for anyone who wants them; putting them in
the surface every harness registers by default makes secrets tooling opt-out
instead of opt-in, and nobody asked for that when the reason to connect was
cross-tool memory.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "axiom-extension.toml"


def _manifest() -> dict:
    return tomllib.loads(MANIFEST.read_text(encoding="utf-8"))


def _tool_names() -> set[str]:
    return {
        p.get("name", "")
        for p in _manifest().get("extension", {}).get("provides", [])
        if p.get("kind") == "tool"
    }


def test_memory_declares_its_read_and_write_tools():
    names = _tool_names()

    assert {"append", "search", "recall"} <= names


def test_declared_names_do_not_repeat_the_extension_prefix():
    """The aggregator prefixes with `axiom_memory__`; declaring the prefix again
    yields `axiom_memory__axiom_memory_append`, which the first version did."""
    assert not any(n.startswith("axiom_memory_") for n in _tool_names())


def test_action_provenance_comes_along():
    assert {"actions_recent", "actions_search"} <= _tool_names()


def test_credential_tools_are_not_in_the_aggregated_surface():
    """The security half. Secrets tooling must be opt-in, not a side effect of
    registering a memory server."""
    names = _tool_names()

    assert "axiom_secrets_list" not in names
    assert "axiom_vault_audit" not in names
    assert "axiom_secrets_rotate_trigger" not in names


def test_every_declared_tool_has_a_resolvable_entry():
    """A declared tool with a broken entry is a stub that looks real."""
    import importlib

    for p in _manifest()["extension"]["provides"]:
        if p.get("kind") != "tool":
            continue
        entry = p.get("entry", "")
        assert ":" in entry, f"{p['name']} has no module:function entry"
        module_path, _, func = entry.partition(":")
        mod = importlib.import_module(module_path)
        assert callable(getattr(mod, func, None)), f"{entry} is not callable"


def test_the_adapters_take_a_single_args_dict():
    """The aggregator calls handlers with one dict; the underlying functions
    take keyword arguments. Without adapters every call is a TypeError."""
    import inspect

    from axiom.extensions.builtins.memory import mcp_server

    sig = inspect.signature(mcp_server.tool_memory_recent)
    assert len(sig.parameters) == 1


def test_an_adapter_drops_unknown_keys_rather_than_erroring():
    """Harnesses send extra fields; a read must not fail over one.

    The principal is passed explicitly rather than relying on a pinned default:
    the first version of this test read `memory.default_principal` from the
    machine it ran on, so it passed locally and failed in CI, which is the
    environment-dependence this suite exists to catch elsewhere.
    """
    from axiom.extensions.builtins.memory import mcp_server

    result = mcp_server.tool_memory_recent(
        {"principal_id": "nobody@example.org", "n": 1, "harness_injected": "x"}
    )

    # The unknown key must not reach the function as a keyword argument; a
    # TypeError is the failure this guards. Any dict back means it was dropped.
    assert isinstance(result, dict)
