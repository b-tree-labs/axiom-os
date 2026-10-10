# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``vault.audit`` payload — the read-only lifecycle audit KEEP surfaces.

Issue #667: one lifecycle surface over two backends — Axiom-minted
capabilities (vault) and foreign-minted secrets. The foreign half is
live (expiry findings from the secrets metadata index); the capability
half reports honestly when its DB is not wired rather than pretending.
Metadata only — no secret values anywhere in the payload.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.secrets.foreign.store import (
    ForeignCredentialStore,
)
from axiom.extensions.builtins.secrets.tests.test_foreign_store import (
    InMemoryValueStore,
)
from axiom.extensions.builtins.vault.audit import audit_payload

NOW = "2026-07-23T12:00:00+00:00"


@pytest.fixture
def seeded_state(tmp_path):
    store = ForeignCredentialStore(tmp_path, value_store=InMemoryValueStore())
    store.set("dead", b"VAL-A", expires_at="2026-07-01")
    store.set("soon", b"VAL-B", expires_at="2026-07-30")
    store.set("fine", b"VAL-C", expires_at="2027-01-01")
    return tmp_path


class TestAuditPayload:
    def test_foreign_findings_present(self, seeded_state):
        payload = audit_payload(state_dir=seeded_state, now=NOW)
        findings = payload["foreign_secrets"]["findings"]
        by_name = {f["name"]: f["level"] for f in findings}
        assert by_name == {"dead": "expired", "soon": "expiring", "fine": "ok"}
        assert payload["foreign_secrets"]["counts"]["expired"] == 1

    def test_capability_section_is_honest(self, seeded_state):
        payload = audit_payload(state_dir=seeded_state, now=NOW)
        caps = payload["capabilities"]
        # Either a real audit (available=True with records) or an honest
        # unavailable report — never a silent omission.
        assert "available" in caps
        if not caps["available"]:
            assert caps["reason"]

    def test_no_values_in_payload(self, seeded_state):
        payload = audit_payload(state_dir=seeded_state, now=NOW)
        blob = json.dumps(payload)
        for sentinel in ("VAL-A", "VAL-B", "VAL-C"):
            assert sentinel not in blob

    def test_empty_state_dir_is_clean(self, tmp_path):
        payload = audit_payload(state_dir=tmp_path, now=NOW)
        assert payload["foreign_secrets"]["findings"] == []
