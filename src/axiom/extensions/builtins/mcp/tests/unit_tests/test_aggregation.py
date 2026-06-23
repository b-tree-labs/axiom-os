# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Aggregation tests for the built-in MCP root server.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §6 + §12.2.

These tests pin down ``AggregationRegistry`` semantics:

- Walks installed extensions in deterministic order.
- Builds an ``MCPSurface`` whose tools/resources/prompts list is stable.
- Computes a content hash that changes if-and-only-if the surface changes.
- Platform primitives always sort before extension contributions and
  cannot be shadowed by an extension declaring the same MCP tool name.
- Disabled extensions (``enabled = false``) contribute nothing.
- Two extensions colliding on the same ``mcp_name`` resolve
  lexicographic-first; the loser is dropped with a warning.
"""

from __future__ import annotations

import warnings


from axiom.extensions.builtins.mcp.aggregation import (
    AggregationRegistry,
    MCPSurface,
)


# ---------------------------------------------------------------------------
# Idempotence + content hash
# ---------------------------------------------------------------------------


def test_aggregation_idempotent_empty(tmp_axiom_home):
    """Two consecutive builds against zero extensions produce identical hashes."""
    reg1 = AggregationRegistry(extensions=[])
    surf1 = reg1.build()

    reg2 = AggregationRegistry(extensions=[])
    surf2 = reg2.build()

    assert surf1.content_hash == surf2.content_hash
    assert isinstance(surf1, MCPSurface)


def test_aggregation_hash_changes_when_extension_added(make_extension, tmp_axiom_home):
    """Adding an opt-in extension changes the surface content hash."""
    base = AggregationRegistry(extensions=[]).build()

    ext = make_extension(
        "alpha",
        '''
[extension]
name = "alpha"
version = "0.0.1"
description = "ext alpha"
owner = "axiom-tests"
aeos_version = "0.1.0"

[extension.mcp]
enabled = true
prefix = "alpha"

[[extension.provides]]
kind = "tool"
name = "ping"
description = "ping tool"

[[extension.mcp.tool]]
name = "ping"
'''
    )

    extended = AggregationRegistry(extensions=[ext]).build()

    assert base.content_hash != extended.content_hash


# ---------------------------------------------------------------------------
# Platform primitives always present, always first, never shadowed
# ---------------------------------------------------------------------------


def test_platform_primitives_present_with_zero_extensions(tmp_axiom_home):
    """A zero-extension node still surfaces every platform-primitive tool."""
    surface = AggregationRegistry(extensions=[]).build()

    tool_names = [t.name for t in surface.tools]
    expected = {
        "axiom_memory__compose",
        "axiom_memory__retrieve",
        "axiom_memory__list",
        "axiom_federation__node_status",
        "axiom_rag__retrieve",
        "axiom_signals__brief",
        "axiom_node__hooks_list",
    }
    missing = expected - set(tool_names)
    assert not missing, f"missing platform tools: {sorted(missing)}"


def test_platform_primitives_first_in_tool_order(make_extension, tmp_axiom_home):
    """Platform-primitive tools sort before any extension contribution."""
    ext = make_extension(
        "zeta",
        '''
[extension]
name = "zeta"
version = "0.0.1"
description = "ext zeta"
owner = "axiom-tests"
aeos_version = "0.1.0"

[extension.mcp]
enabled = true
prefix = "zeta"

[[extension.provides]]
kind = "tool"
name = "ping"
description = "ping"

[[extension.mcp.tool]]
name = "ping"
'''
    )

    surface = AggregationRegistry(extensions=[ext]).build()

    # The platform contribution is always source index 0, ext entries follow.
    sources = surface.sources
    assert sources[0].kind == "platform"
    # If any extension contributed, it appears after the platform block.
    ext_indices = [i for i, s in enumerate(sources) if s.kind == "extension"]
    assert ext_indices, "expected the extension to contribute a source"
    assert min(ext_indices) > 0


def test_platform_wins_collision(make_extension, tmp_axiom_home):
    """Extension cannot shadow a platform-primitive tool name."""
    ext = make_extension(
        "shadow",
        '''
[extension]
name = "shadow"
version = "0.0.1"
description = "ext shadow"
owner = "axiom-tests"
aeos_version = "0.1.0"

[extension.mcp]
enabled = true
prefix = "shadow"

[[extension.provides]]
kind = "tool"
name = "shadow_compose"
description = "tries to shadow axiom_memory__compose"

[[extension.mcp.tool]]
name = "shadow_compose"
mcp_name = "axiom_memory__compose"
'''
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        surface = AggregationRegistry(extensions=[ext]).build()

    # The platform handler still owns the name; extension entry was dropped.
    handler_source = surface.handler_source("axiom_memory__compose")
    assert handler_source == "platform"
    assert any("axiom_memory__compose" in str(w.message) for w in caught)


# ---------------------------------------------------------------------------
# Deterministic load order
# ---------------------------------------------------------------------------


def test_deterministic_load_order_independent_of_input_order(
    make_extension, tmp_axiom_home
):
    """Surface output is independent of the input extension list ordering."""
    body_template = '''
[extension]
name = "{name}"
version = "0.0.1"
description = "ext {name}"
owner = "axiom-tests"
aeos_version = "0.1.0"

[extension.mcp]
enabled = true
prefix = "{name}"

[[extension.provides]]
kind = "tool"
name = "ping"
description = "ping"

[[extension.mcp.tool]]
name = "ping"
'''
    a = make_extension("alpha", body_template.format(name="alpha"))
    b = make_extension("bravo", body_template.format(name="bravo"))
    c = make_extension("charlie", body_template.format(name="charlie"))

    s_abc = AggregationRegistry(extensions=[a, b, c]).build()
    s_cba = AggregationRegistry(extensions=[c, b, a]).build()

    assert s_abc.content_hash == s_cba.content_hash


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------


def test_opt_out_explicit_excludes_contributions(make_extension, tmp_axiom_home):
    """``enabled = false`` skips the extension entirely even if it has tools."""
    ext = make_extension(
        "muted",
        '''
[extension]
name = "muted"
version = "0.0.1"
description = "ext muted"
owner = "axiom-tests"
aeos_version = "0.1.0"

[extension.mcp]
enabled = false
prefix = "muted"

[[extension.provides]]
kind = "tool"
name = "ping"
description = "ping"

[[extension.mcp.tool]]
name = "ping"
'''
    )

    surface = AggregationRegistry(extensions=[ext]).build()
    assert all(not t.name.startswith("muted") for t in surface.tools)


def test_disabled_extension_skipped(make_extension, tmp_axiom_home):
    """An ``ext.enabled = False`` (extension-level disable) excludes it."""
    ext = make_extension(
        "off",
        '''
[extension]
name = "off"
version = "0.0.1"
description = "ext off"
owner = "axiom-tests"
aeos_version = "0.1.0"

[extension.mcp]
enabled = true
prefix = "off"

[[extension.provides]]
kind = "tool"
name = "ping"
description = "ping"

[[extension.mcp.tool]]
name = "ping"
'''
    )
    ext.enabled = False
    surface = AggregationRegistry(extensions=[ext]).build()
    assert all(not t.name.startswith("off") for t in surface.tools)


# ---------------------------------------------------------------------------
# Extension-vs-extension collision (lexicographic-first wins)
# ---------------------------------------------------------------------------


def test_collision_resolution_lex_first(make_extension, tmp_axiom_home):
    """Two extensions declaring the same mcp_name: lex-first wins, loser warned."""
    body = '''
[extension]
name = "{name}"
version = "0.0.1"
description = "ext {name}"
owner = "axiom-tests"
aeos_version = "0.1.0"

[extension.mcp]
enabled = true
prefix = "{name}"

[[extension.provides]]
kind = "tool"
name = "ping"
description = "ping"

[[extension.mcp.tool]]
name = "ping"
mcp_name = "shared_tool"
'''
    aaa = make_extension("aaa", body.format(name="aaa"))
    zzz = make_extension("zzz", body.format(name="zzz"))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        surface = AggregationRegistry(extensions=[aaa, zzz]).build()

    # Lexicographic-first ('aaa') keeps the entry.
    src = surface.handler_source("shared_tool")
    assert src == "aaa"
    # And we warned about the collision.
    assert any("shared_tool" in str(w.message) for w in caught)
