# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An install must not require a Rust toolchain on an Intel Mac.

cryptography 49.0.0 stopped publishing a macOS x86_64 wheel — from that release
the only macOS wheel is `macosx_11_0_arm64`. On an Intel Mac (or an x86_64
Python under Rosetta) pip then finds no wheel for a base dependency, falls back
to building from source, and the install dies several hundred lines into a cargo
build asking for OpenSSL headers and pkg-config.

That is the first thing a new adopter runs, so it is the whole product to them.
The cap is platform-scoped so every other platform keeps current crypto.
"""

from __future__ import annotations

import pathlib
import tomllib

from packaging.requirements import Requirement

INTEL_MAC = {"platform_system": "Darwin", "platform_machine": "x86_64"}
APPLE_SILICON = {"platform_system": "Darwin", "platform_machine": "arm64"}
LINUX = {"platform_system": "Linux", "platform_machine": "x86_64"}

# The last cryptography release that ships a macOS x86_64 (universal2) wheel.
FIRST_ARM64_ONLY_RELEASE = 49


def _dependencies() -> list[Requirement]:
    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return [Requirement(d) for d in data["project"]["dependencies"]]


def _crypto_caps(env: dict[str, str]) -> list[Requirement]:
    """cryptography requirements that apply in `env` and set an upper bound."""
    return [
        r
        for r in _dependencies()
        if r.name == "cryptography"
        and (r.marker is None or r.marker.evaluate(env))
        and any(s.operator in ("<", "<=", "==") for s in r.specifier)
    ]


def test_intel_mac_is_capped_below_the_arm64_only_releases():
    caps = _crypto_caps(INTEL_MAC)

    assert caps, (
        "no upper bound on cryptography for Darwin/x86_64 — an Intel Mac will "
        "resolve to an arm64-only release and try to compile Rust"
    )
    assert any(
        s.version.startswith(str(FIRST_ARM64_ONLY_RELEASE)) or s.operator == "<="
        for cap in caps
        for s in cap.specifier
        if s.operator in ("<", "<=")
    )
    # The cap has to actually exclude the release that broke it.
    assert not any(cap.specifier.contains("49.0.0") for cap in caps)
    assert not any(cap.specifier.contains("50.0.1") for cap in caps)


def test_the_cap_is_scoped_to_the_platform_that_needs_it():
    """Negative control: pinning crypto everywhere would be the wrong fix."""
    assert _crypto_caps(APPLE_SILICON) == []
    assert _crypto_caps(LINUX) == []


def test_every_platform_still_gets_a_floor():
    """The cap must not have replaced the minimum-version requirement."""
    floors = [
        r
        for r in _dependencies()
        if r.name == "cryptography"
        and any(s.operator in (">=", ">", "==") for s in r.specifier)
    ]

    assert floors, "cryptography lost its lower bound"
    for env in (INTEL_MAC, APPLE_SILICON, LINUX):
        assert any(f.marker is None or f.marker.evaluate(env) for f in floors)
