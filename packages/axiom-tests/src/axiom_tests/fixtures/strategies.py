# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``hypothesis_strategies`` fixture — Hypothesis strategies for AEOS types.

Provides ready-made Hypothesis strategies for property-based testing of
AEOS-relevant types: extension names, semver versions, capability blocks,
manifests, and signal/event names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import strategies as st

# --- Low-level primitives -------------------------------------------------


def extension_names() -> st.SearchStrategy[str]:
    """Valid extension/package names per AEOS §5.4.

    Purpose-named: ``^[a-z][a-z0-9_]*$``, no hyphens, no type suffix.
    We don't try to enforce the "no type suffix" rule here — that is a
    linter concern, not a shape concern.
    """
    return st.from_regex(r"\A[a-z][a-z0-9_]{0,31}\Z", fullmatch=True)


def semver_versions() -> st.SearchStrategy[str]:
    """Simple semver 2.0.0 versions (``MAJOR.MINOR.PATCH``)."""
    return st.tuples(
        st.integers(min_value=0, max_value=99),
        st.integers(min_value=0, max_value=99),
        st.integers(min_value=0, max_value=999),
    ).map(lambda triple: f"{triple[0]}.{triple[1]}.{triple[2]}")


def aeos_versions() -> st.SearchStrategy[str]:
    """Version strings matching the AEOS ``aeos_version`` pattern."""
    return st.sampled_from(["0.1", "0.1.0", "0.2", "0.2.0", "1.0.0"])


def classification_levels() -> st.SearchStrategy[str]:
    return st.sampled_from(["public", "internal", "restricted", "secret", "top_secret"])


def side_effects() -> st.SearchStrategy[str]:
    return st.sampled_from(
        ["none", "reads_file", "writes_file", "calls_network", "local_only", "mutates_state"]
    )


def fail_modes() -> st.SearchStrategy[str]:
    return st.sampled_from(["abort", "warn", "ignore"])


def event_names() -> st.SearchStrategy[str]:
    """Event names used by hook capabilities, e.g. ``session.started``."""
    segment = st.from_regex(r"\A[a-z][a-z0-9_]*\Z", fullmatch=True)
    return st.tuples(segment, segment).map(lambda pair: ".".join(pair))


def entry_specs() -> st.SearchStrategy[str]:
    """Python entry-point strings of the form ``pkg.mod:Obj``."""
    pkg = st.from_regex(r"\A[a-z][a-z0-9_]*\Z", fullmatch=True)
    mod = st.from_regex(r"\A[a-z][a-z0-9_]*\Z", fullmatch=True)
    obj = st.from_regex(r"\A[A-Z][A-Za-z0-9_]*\Z", fullmatch=True)
    return st.tuples(pkg, mod, obj).map(lambda t: f"{t[0]}.{t[1]}:{t[2]}")


# --- Composite strategies -------------------------------------------------


def provided_tools() -> st.SearchStrategy[dict[str, Any]]:
    return st.builds(
        lambda name, entry, idempotent, fx: {
            "kind": "tool",
            "name": name,
            "entry": entry,
            "description": f"Test tool {name}",
            "idempotent": idempotent,
            "side_effects": fx,
        },
        name=extension_names(),
        entry=entry_specs(),
        idempotent=st.booleans(),
        fx=side_effects(),
    )


def minimal_manifests() -> st.SearchStrategy[dict[str, Any]]:
    """A minimally valid AEOS manifest with one tool capability."""
    return st.builds(
        lambda name, version, desc, aeos_v, tool: {
            "extension": {
                "name": name,
                "version": version,
                "description": desc or f"Test extension {name}",
                "license": "Apache-2.0",
                "aeos_version": aeos_v,
                "provides": [tool],
            }
        },
        name=extension_names(),
        version=semver_versions(),
        desc=st.text(min_size=1, max_size=80),
        aeos_v=aeos_versions(),
        tool=provided_tools(),
    )


@dataclass(frozen=True)
class AEOSStrategies:
    """Bundle of Hypothesis strategies exposed via a single fixture."""

    extension_names: st.SearchStrategy[str]
    semver_versions: st.SearchStrategy[str]
    aeos_versions: st.SearchStrategy[str]
    classification_levels: st.SearchStrategy[str]
    side_effects: st.SearchStrategy[str]
    fail_modes: st.SearchStrategy[str]
    event_names: st.SearchStrategy[str]
    entry_specs: st.SearchStrategy[str]
    provided_tools: st.SearchStrategy[dict[str, Any]]
    minimal_manifests: st.SearchStrategy[dict[str, Any]]


@pytest.fixture(scope="session")
def hypothesis_strategies() -> AEOSStrategies:
    """Session-scoped bundle of AEOS Hypothesis strategies."""
    return AEOSStrategies(
        extension_names=extension_names(),
        semver_versions=semver_versions(),
        aeos_versions=aeos_versions(),
        classification_levels=classification_levels(),
        side_effects=side_effects(),
        fail_modes=fail_modes(),
        event_names=event_names(),
        entry_specs=entry_specs(),
        provided_tools=provided_tools(),
        minimal_manifests=minimal_manifests(),
    )


__all__ = [
    "AEOSStrategies",
    "aeos_versions",
    "classification_levels",
    "entry_specs",
    "event_names",
    "extension_names",
    "fail_modes",
    "hypothesis_strategies",
    "minimal_manifests",
    "provided_tools",
    "semver_versions",
    "side_effects",
]
