# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for axiom.infra.audit_log.

Run:
    pytest tests/infra/test_audit_log.py -v
"""

from __future__ import annotations

import hashlib
import hmac as hmac_lib
import json
import uuid

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_audit(tmp_path, mode="standard", backend="jsonl", hmac_key=None):
    """Construct an AuditLog instance in a controlled test environment."""
    from axiom.infra.audit_log import AuditLog
    log_dir = tmp_path / "logs" / "audit"
    a = AuditLog(log_dir=log_dir, hmac_key=hmac_key)
    a.set_mode(mode)
    # Only force jsonl backend when in ec mode (standard mode uses NullBackend)
    if backend == "jsonl" and mode == "ec":
        a._force_jsonl_backend()
    return a


# ---------------------------------------------------------------------------
# TestAuditLogMode
# ---------------------------------------------------------------------------

class TestAuditLogMode:

    def test_default_mode_is_standard(self, tmp_path):
        from axiom.infra.audit_log import AuditLog
        a = AuditLog(log_dir=tmp_path)
        assert a.mode == "standard"

    def test_set_mode_ec(self, tmp_path):
        from axiom.infra.audit_log import AuditLog
        a = AuditLog(log_dir=tmp_path, hmac_key="test-key")
        a.set_mode("ec")
        assert a.mode == "ec"

    def test_set_mode_rejects_unknown(self, tmp_path):
        from axiom.infra.audit_log import AuditLog
        a = AuditLog(log_dir=tmp_path)
        with pytest.raises(ValueError):
            a.set_mode("invalid")

    def test_get_returns_singleton(self, tmp_path, monkeypatch):
        from axiom.infra import audit_log
        monkeypatch.setattr(audit_log, "_instance", None)
        a1 = audit_log.AuditLog.get()
        a2 = audit_log.AuditLog.get()
        assert a1 is a2


# ---------------------------------------------------------------------------
# TestNullBackend — standard mode write_* are no-ops
# ---------------------------------------------------------------------------

class TestNullBackend:

    def test_write_classification_noop_in_standard_mode(self, tmp_path):
        a = _make_audit(tmp_path, mode="standard")
        # Should not raise, should not create any files
        a.write_classification(
            routing_event_id=str(uuid.uuid4()),
            prompt_hash="abc",
            classifier="keyword",
            keyword_matched=False,
            keyword_term=None,
            ollama_result=None,
            sensitivity="standard",
            final_tier="public",
            is_ec=False,
        )
        log_dir = tmp_path / "logs" / "audit"
        assert not log_dir.exists() or not any(log_dir.iterdir())

    def test_write_routing_noop_in_standard_mode(self, tmp_path):
        a = _make_audit(tmp_path, mode="standard")
        a.write_routing(
            session_id="sess-1",
            tier_requested="public",
            tier_assigned="public",
            provider_name="llm-private",
            provider_tier="public",
            blocked=False,
            block_reason=None,
            prompt_hash="abc",
            response_hash="def",
            ec_violation=False,
            is_ec=False,
        )
        log_dir = tmp_path / "logs" / "audit"
        assert not log_dir.exists() or not any(log_dir.iterdir())

    def test_backend_info_standard_mode(self, tmp_path):
        a = _make_audit(tmp_path, mode="standard")
        info = a.backend_info()
        assert info.backend == "null"


# ---------------------------------------------------------------------------
# TestJsonlBackend
# ---------------------------------------------------------------------------

class TestJsonlBackend:

    def test_write_routing_creates_file(self, tmp_path):
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        a.write_routing(
            session_id="sess-1",
            tier_requested="export_controlled",
            tier_assigned="export_controlled",
            provider_name="llm-private",
            provider_tier="export_controlled",
            blocked=False,
            block_reason=None,
            prompt_hash="abc123",
            response_hash="def456",
            ec_violation=False,
            is_ec=True,
        )
        log_dir = tmp_path / "logs" / "audit"
        files = list(log_dir.glob("routing_events*.jsonl"))
        assert files

    def test_write_routing_record_is_valid_json(self, tmp_path):
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        a.write_routing(
            session_id="sess-1",
            tier_requested="export_controlled",
            tier_assigned="export_controlled",
            provider_name="llm-private",
            provider_tier="export_controlled",
            blocked=False,
            block_reason=None,
            prompt_hash="abc123",
            response_hash=None,
            ec_violation=False,
            is_ec=True,
        )
        log_dir = tmp_path / "logs" / "audit"
        lines = (log_dir / "routing_events.jsonl").read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["provider_name"] == "llm-private"
        assert record["session_id"] == "sess-1"

    def test_write_classification_record_fields(self, tmp_path):
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        rid = str(uuid.uuid4())
        a.write_classification(
            routing_event_id=rid,
            prompt_hash="ph",
            classifier="keyword",
            keyword_matched=True,
            keyword_term="enrichment",
            ollama_result=None,
            sensitivity="strict",
            final_tier="export_controlled",
            is_ec=True,
        )
        log_dir = tmp_path / "logs" / "audit"
        record = json.loads((log_dir / "classification_events.jsonl").read_text())
        assert record["keyword_term"] == "enrichment"
        assert record["routing_event_id"] == rid

    def test_write_vpn_record_fields(self, tmp_path):
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        rid = str(uuid.uuid4())
        a.write_vpn(
            routing_event_id=rid,
            provider_name="llm-private",
            vpn_reachable=False,
            check_duration_ms=42,
        )
        log_dir = tmp_path / "logs" / "audit"
        record = json.loads((log_dir / "vpn_events.jsonl").read_text())
        assert record["vpn_reachable"] is False
        assert record["check_duration_ms"] == 42

    def test_write_config_load_record_fields(self, tmp_path):
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        a.write_config_load(
            config_file="llm-providers.toml",
            providers=[{"name": "llm-private"}],
            ec_providers_count=1,
        )
        log_dir = tmp_path / "logs" / "audit"
        record = json.loads((log_dir / "config_load_events.jsonl").read_text())
        assert record["config_file"] == "llm-providers.toml"
        assert record["ec_providers_count"] == 1

    def test_multiple_writes_append_lines(self, tmp_path):
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        for i in range(3):
            a.write_routing(
                session_id=f"sess-{i}",
                tier_requested="export_controlled",
                tier_assigned="export_controlled",
                provider_name="llm-private",
                provider_tier="export_controlled",
                blocked=False,
                block_reason=None,
                prompt_hash=f"ph-{i}",
                response_hash=None,
                ec_violation=False,
                is_ec=True,
            )
        log_dir = tmp_path / "logs" / "audit"
        lines = (log_dir / "routing_events.jsonl").read_text().splitlines()
        assert len(lines) == 3

    def test_backend_info_jsonl(self, tmp_path):
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        info = a.backend_info()
        assert info.backend == "jsonl"


# ---------------------------------------------------------------------------
# TestHmacChain
# ---------------------------------------------------------------------------

class TestHmacChain:

    def test_routing_record_has_hmac_field(self, tmp_path):
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        a.write_routing(
            session_id="s", tier_requested="ec", tier_assigned="ec",
            provider_name="p", provider_tier="ec", blocked=False,
            block_reason=None, prompt_hash="ph", response_hash=None,
            ec_violation=False, is_ec=True,
        )
        log_dir = tmp_path / "logs" / "audit"
        record = json.loads((log_dir / "routing_events.jsonl").read_text())
        assert "hmac" in record
        assert len(record["hmac"]) == 64  # SHA-256 hex

    def test_verify_chain_passes_on_intact_file(self, tmp_path):
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        for i in range(3):
            a.write_routing(
                session_id="s", tier_requested="ec", tier_assigned="ec",
                provider_name="p", provider_tier="ec", blocked=False,
                block_reason=None, prompt_hash=f"ph{i}", response_hash=None,
                ec_violation=False, is_ec=True,
            )
        ok, broken_at = a.verify_chain(table="routing_events")
        assert ok is True
        assert broken_at is None

    def test_verify_chain_detects_tampered_record(self, tmp_path):
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        for i in range(3):
            a.write_routing(
                session_id="s", tier_requested="ec", tier_assigned="ec",
                provider_name="p", provider_tier="ec", blocked=False,
                block_reason=None, prompt_hash=f"ph{i}", response_hash=None,
                ec_violation=False, is_ec=True,
            )
        log_dir = tmp_path / "logs" / "audit"
        path = log_dir / "routing_events.jsonl"
        lines = path.read_text().splitlines()
        # Tamper with the second record
        rec = json.loads(lines[1])
        rec["provider_name"] = "TAMPERED"
        lines[1] = json.dumps(rec)
        path.write_text("\n".join(lines) + "\n")

        ok, broken_at = a.verify_chain(table="routing_events")
        assert ok is False
        assert broken_at == 1  # 0-indexed second record

    def test_first_record_uses_genesis_prev_hmac(self, tmp_path):
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        a.write_routing(
            session_id="s", tier_requested="ec", tier_assigned="ec",
            provider_name="p", provider_tier="ec", blocked=False,
            block_reason=None, prompt_hash="ph", response_hash=None,
            ec_violation=False, is_ec=True,
        )
        log_dir = tmp_path / "logs" / "audit"
        record = json.loads((log_dir / "routing_events.jsonl").read_text())
        assert "hmac" in record
        # Verify the HMAC ourselves
        body = {k: v for k, v in record.items() if k != "hmac"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        expected = hmac_lib.new(
            b"test-key",
            (canonical + "GENESIS").encode(),
            hashlib.sha256,
        ).hexdigest()
        assert record["hmac"] == expected

    def test_verify_chain_empty_file_returns_ok(self, tmp_path):
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        ok, broken_at = a.verify_chain(table="routing_events")
        assert ok is True
        assert broken_at is None


# ---------------------------------------------------------------------------
# TestEcModeGuards
# ---------------------------------------------------------------------------

class TestEcModeGuards:

    def test_ec_mode_requires_hmac_key(self, tmp_path):
        from axiom.infra.audit_log import AuditLog
        a = AuditLog(log_dir=tmp_path, hmac_key=None)
        with pytest.raises(ValueError, match="[Hh][Mm][Aa][Cc]"):
            a.set_mode("ec")

    def test_is_ec_record_not_written_in_standard_mode(self, tmp_path):
        """EC-flagged writes are accepted but treated as no-ops in standard mode."""
        a = _make_audit(tmp_path, mode="standard")
        a.write_routing(
            session_id="s", tier_requested="ec", tier_assigned="ec",
            provider_name="p", provider_tier="ec", blocked=False,
            block_reason=None, prompt_hash="ph", response_hash=None,
            ec_violation=False, is_ec=True,
        )
        log_dir = tmp_path / "logs" / "audit"
        assert not log_dir.exists() or not any(log_dir.iterdir())

    def test_ec_violation_raises_after_write(self, tmp_path):
        from axiom.infra.audit_log import ECViolationError
        a = _make_audit(tmp_path, mode="ec", backend="jsonl", hmac_key="test-key")
        with pytest.raises(ECViolationError):
            a.write_routing(
                session_id="s", tier_requested="ec", tier_assigned="public",
                provider_name="p", provider_tier="public", blocked=False,
                block_reason=None, prompt_hash="ph", response_hash=None,
                ec_violation=True, is_ec=True,
            )
        # Record must still have been written before the raise
        log_dir = tmp_path / "logs" / "audit"
        lines = (log_dir / "routing_events.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["ec_violation"] is True
