# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`axiom.cli.ext._spdx`."""

from __future__ import annotations

import pytest

from axiom.cli.ext._spdx import ALLOWLIST, allowlist_hint, resolve_spdx


class TestCanonical:
    """Canonical SPDX ids round-trip unchanged."""

    @pytest.mark.parametrize("canonical", ALLOWLIST)
    def test_canonical_passes_through(self, canonical: str):
        assert resolve_spdx(canonical) == canonical

    def test_case_insensitive_canonical(self):
        assert resolve_spdx("APACHE-2.0") == "Apache-2.0"
        assert resolve_spdx("mit") == "MIT"


class TestAliases:
    """Short aliases resolve to canonical form."""

    @pytest.mark.parametrize(
        "shorthand, expected",
        [
            ("apache", "Apache-2.0"),
            ("apache2", "Apache-2.0"),
            ("apache-2", "Apache-2.0"),
            ("Apache2", "Apache-2.0"),
            ("MIT", "MIT"),
            ("mit", "MIT"),
            ("bsd", "BSD-3-Clause"),
            ("bsd2", "BSD-2-Clause"),
            ("bsd3", "BSD-3-Clause"),
            ("bsd-3", "BSD-3-Clause"),
            ("mpl", "MPL-2.0"),
            ("mpl2", "MPL-2.0"),
            ("lgpl", "LGPL-3.0"),
            ("gpl", "GPL-3.0"),
            ("isc", "ISC"),
            ("unlicense", "Unlicense"),
        ],
    )
    def test_alias_resolves(self, shorthand: str, expected: str):
        assert resolve_spdx(shorthand) == expected


class TestRejection:
    """Unknown inputs return ``None`` so callers can emit a clear error."""

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "Proprietary-Internal",
            "LicenseRef-NotReal",
            "whatever",
            "AGPL-3.0",  # AGPL not on v0.1 allowlist
            "CC-BY-4.0",  # CC not on v0.1 allowlist
        ],
    )
    def test_rejects_unknown(self, bad: str):
        assert resolve_spdx(bad) is None


def test_allowlist_hint_lists_known_ids():
    hint = allowlist_hint()
    for canonical in ALLOWLIST:
        assert canonical in hint
