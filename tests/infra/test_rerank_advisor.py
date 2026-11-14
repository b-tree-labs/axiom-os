# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for #82 rerank-upgrade advisor.

Detects when a node has outgrown RRF-only retrieval and should install
``axiom[rerank]``. Emits an advisory (not an auto-install). Thresholds
come from ``project_rerank_upgrade_threshold.md``:

    - Corpus chunks ≥ 5,000
    - Role is classroom host / federation server / platform node
    - Any chunk with access_tier ≠ public OR classification ≠ unclassified
    - Sustained query volume ≥ 50/day over 7 days
"""

from __future__ import annotations

import pytest

from axiom.infra.rerank_advisor import (
    UpgradeAdvisory,
    check_rerank_upgrade,
    format_upgrade_message,
)


def _state(**overrides) -> dict:
    """Build a NodeState dict with sane defaults for a small leaf node."""
    base = {
        "corpus_chunks": 100,
        "role": "leaf",              # leaf | classroom | server | platform
        "has_non_public_tier": False,
        "queries_last_7d": 10,
        "rerank_already_installed": False,
    }
    base.update(overrides)
    return base


class TestNoUpgradeNeeded:
    def test_small_leaf_node_no_advisory(self):
        result = check_rerank_upgrade(_state())
        assert isinstance(result, UpgradeAdvisory)
        assert result.recommended is False
        assert result.reasons == []

    def test_already_installed_short_circuits(self):
        """Even if threshold crossed, no advisory when rerank is already installed."""
        result = check_rerank_upgrade(_state(
            corpus_chunks=100000, rerank_already_installed=True,
        ))
        assert result.recommended is False


class TestCorpusSize:
    def test_5000_chunks_triggers(self):
        result = check_rerank_upgrade(_state(corpus_chunks=5000))
        assert result.recommended is True
        assert any("corpus" in r.lower() for r in result.reasons)

    def test_just_under_does_not_trigger(self):
        result = check_rerank_upgrade(_state(corpus_chunks=4999))
        assert result.recommended is False


class TestRoleGate:
    @pytest.mark.parametrize("role", ["classroom", "server", "platform"])
    def test_serving_roles_trigger(self, role):
        result = check_rerank_upgrade(_state(role=role))
        assert result.recommended is True
        assert any("role" in r.lower() for r in result.reasons)

    @pytest.mark.parametrize("role", ["leaf", "personal", "unknown"])
    def test_non_serving_roles_do_not_trigger(self, role):
        result = check_rerank_upgrade(_state(role=role))
        assert result.recommended is False


class TestSensitivityGate:
    def test_non_public_tier_triggers(self):
        result = check_rerank_upgrade(_state(has_non_public_tier=True))
        assert result.recommended is True
        assert any("tier" in r.lower() or "sensitiv" in r.lower() for r in result.reasons)


class TestVolumeGate:
    def test_50_qpd_over_7d_triggers(self):
        result = check_rerank_upgrade(_state(queries_last_7d=350))  # 50/day
        assert result.recommended is True
        assert any("quer" in r.lower() or "volume" in r.lower() for r in result.reasons)

    def test_49_per_day_does_not_trigger(self):
        result = check_rerank_upgrade(_state(queries_last_7d=342))  # <50/day
        assert result.recommended is False


class TestCompositeReasons:
    def test_multiple_gates_all_surfaced(self):
        result = check_rerank_upgrade(_state(
            corpus_chunks=10000, role="classroom",
        ))
        assert result.recommended is True
        # Both reasons should surface so the user sees the full picture.
        joined = " ".join(result.reasons).lower()
        assert "corpus" in joined
        assert "role" in joined


class TestUserMessage:
    def test_message_includes_install_command(self):
        advisory = UpgradeAdvisory(
            recommended=True, reasons=["corpus has grown past 5,000 chunks"],
        )
        msg = format_upgrade_message(advisory)
        assert "pip install" in msg
        assert "axiom[rerank]" in msg
        assert "corpus" in msg.lower()

    def test_empty_when_not_recommended(self):
        assert format_upgrade_message(UpgradeAdvisory(recommended=False)) == ""
