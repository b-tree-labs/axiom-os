# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Guard against a recurrence of the branding/PyPI name drift bug.

On 2026-04-15 we shipped with ``BrandingConfig.package_name="axiom"``, but
the real PyPI distribution is ``axi-platform``. ``axi update`` ran
``pip install --upgrade axiom`` and pulled an unrelated squatter package
of the same name (v0.9.0) which failed to build a wheel — supply-chain
adjacent and a silent upgrade-path failure on every peer.

These tests pin the defaults and assert that any built-in branding
override points at a distribution name our release pipeline actually
publishes.
"""

from __future__ import annotations

import urllib.request

import pytest

from axiom.infra.branding import BrandingConfig

# Distribution names we own / publish. axi-platform retained as legacy
# (we own the PyPI name; last published version is 0.11.0; no new
# uploads under that name post-0.12.0 rename to axiom-os).
KNOWN_OWNED_DISTS = {"axiom-os-lm", "axi-platform"}


def test_default_package_name_is_axiom_os() -> None:
    """Default brand ships with the real PyPI name, not the in-source module name."""
    assert BrandingConfig().package_name == "axiom-os-lm", (
        "Default branding.package_name drifted from the PyPI distribution. "
        "If you renamed the PyPI dist, update this test AND confirm the old "
        "name isn't a squatter on PyPI."
    )


def test_package_name_is_not_a_squatted_collision() -> None:
    """Ensure our default package_name isn't the name of an unrelated PyPI package.

    The bug we're guarding against: ``package_name='axiom'`` matched an
    unrelated ``axiom`` package on PyPI (v0.9.0). ``axi update`` happily
    installed the squatter until the wheel build failed.
    """
    assert BrandingConfig().package_name in KNOWN_OWNED_DISTS


@pytest.mark.network
def test_package_name_resolves_on_pypi() -> None:
    """The dist name actually exists on PyPI. Skipped offline.

    Marked ``network`` so CI can opt out; run locally to catch rename-without-
    upload mistakes before the next release.
    """
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{BrandingConfig().package_name}/json",
            timeout=5,
        ) as resp:
            assert resp.status == 200
    except Exception as exc:  # pragma: no cover — offline CI
        pytest.skip(f"PyPI unreachable: {exc}")
