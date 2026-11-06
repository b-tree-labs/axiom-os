# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federation URI scheme — axiom://<node-id>/<fragment-id>.

Per ADR-027. Every cross-node fragment reference uses this scheme.
Works for both the MVP central-coordinator resolver and a future
DHT resolver without callers needing to change.

Design goals:
- Self-contained: the URI tells you which node to ask.
- DHT-friendly: the fragment-id component is content-addressable.
- Human-readable: debuggable at a glance.
"""

from __future__ import annotations

from urllib.parse import urlparse

_SCHEME = "axiom"


def format_uri(node_id: str, fragment_id: str) -> str:
    """Assemble an axiom:// URI from node and fragment components."""
    if not node_id:
        raise ValueError("node id is required")
    if not fragment_id:
        raise ValueError("fragment id is required")
    return f"{_SCHEME}://{node_id}/{fragment_id}"


def parse_uri(uri: str) -> tuple[str, str]:
    """Parse an axiom:// URI into (node_id, fragment_id)."""
    parsed = urlparse(uri)
    if parsed.scheme != _SCHEME:
        raise ValueError(f"expected scheme 'axiom://'; got {parsed.scheme!r}")
    node = parsed.netloc
    # Path looks like "/fragment-id" — strip the leading slash
    path = parsed.path.lstrip("/")
    if not node:
        raise ValueError(f"uri missing node component: {uri!r}")
    if not path:
        raise ValueError(f"uri missing fragment component: {uri!r}")
    return node, path


def is_axiom_uri(s: str) -> bool:
    """True iff the string parses as a valid axiom:// URI."""
    try:
        parse_uri(s)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Bare node coordinates — axiom://<node-id> (no fragment component).
#
# A cross-mem sync *peer* is a whole node, addressed by its node id alone
# (ADR-087 D2 hub-and-spoke: the node's store is the reconciliation point).
# This is distinct from a fragment reference axiom://<node-id>/<fragment-id>.
# ---------------------------------------------------------------------------


def format_node_uri(node_id: str) -> str:
    """Assemble a bare node coordinate axiom://<node-id> (no fragment)."""
    if not node_id:
        raise ValueError("node id is required")
    return f"{_SCHEME}://{node_id}"


def parse_node_uri(uri: str) -> str:
    """Parse a bare axiom://<node-id> coordinate into its node id.

    Raises ``ValueError`` if a fragment component is present (that is a
    fragment reference, not a node coordinate) or the scheme/node is missing.
    """
    parsed = urlparse(uri)
    if parsed.scheme != _SCHEME:
        raise ValueError(f"expected scheme 'axiom://'; got {parsed.scheme!r}")
    node = parsed.netloc
    if not node:
        raise ValueError(f"uri missing node component: {uri!r}")
    if parsed.path.strip("/"):
        raise ValueError(f"node coordinate must have no fragment component: {uri!r}")
    return node


def is_node_uri(s: str) -> bool:
    """True iff the string parses as a bare axiom://<node-id> coordinate."""
    try:
        parse_node_uri(s)
    except ValueError:
        return False
    return True
