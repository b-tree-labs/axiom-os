# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the signal router."""

import pytest

from axiom.extensions.builtins.signals.models import Signal
from axiom.extensions.builtins.signals.router import Endpoint, Router, TransitRecord


class TestEndpoint:
    """Test Endpoint dataclass and matching logic."""

    def test_default_endpoint_matches_all(self):
        endpoint = Endpoint(id="test", name="Test Endpoint")

        signal = Signal(
            source="voice",
            timestamp="2026-02-15T10:00:00Z",
            raw_text="hello world",
            signal_type="progress",
            confidence=0.8,
        )

        assert endpoint.matches(signal) is True

    def test_disabled_endpoint_matches_nothing(self):
        endpoint = Endpoint(id="test", name="Test", enabled=False)

        signal = Signal(
            source="voice",
            timestamp="2026-02-15T10:00:00Z",
            raw_text="hello",
        )

        assert endpoint.matches(signal) is False

    def test_confidence_filter_min(self):
        endpoint = Endpoint(id="test", name="Test", min_confidence=0.7)

        low_conf = Signal(source="voice", timestamp="2026-02-15T10:00:00Z",
                          raw_text="a", confidence=0.5)
        high_conf = Signal(source="voice", timestamp="2026-02-15T10:00:00Z",
                           raw_text="b", confidence=0.9)

        assert endpoint.matches(low_conf) is False
        assert endpoint.matches(high_conf) is True

    def test_confidence_filter_max(self):
        endpoint = Endpoint(id="test", name="Test", max_confidence=0.6)

        low_conf = Signal(source="voice", timestamp="2026-02-15T10:00:00Z",
                          raw_text="a", confidence=0.5)
        high_conf = Signal(source="voice", timestamp="2026-02-15T10:00:00Z",
                           raw_text="b", confidence=0.9)

        assert endpoint.matches(low_conf) is True
        assert endpoint.matches(high_conf) is False

    def test_signal_type_filter(self):
        endpoint = Endpoint(
            id="blockers",
            name="Blocker Collector",
            signal_types=["blocker", "decision"],
        )

        blocker = Signal(source="voice", timestamp="2026-02-15T10:00:00Z",
                         raw_text="a", signal_type="blocker")
        progress = Signal(source="voice", timestamp="2026-02-15T10:00:00Z",
                          raw_text="b", signal_type="progress")

        assert endpoint.matches(blocker) is True
        assert endpoint.matches(progress) is False

    def test_initiative_filter(self):
        endpoint = Endpoint(
            id="alpha",
            name="Alpha Channel",
            initiatives=["Project Alpha", "Project Beta"],
        )

        alpha_signal = Signal(
            source="voice", timestamp="2026-02-15T10:00:00Z", raw_text="a",
            initiatives=["Project Alpha"],
        )
        other_signal = Signal(
            source="voice", timestamp="2026-02-15T10:00:00Z", raw_text="b",
            initiatives=["Project Gamma"],
        )
        no_init = Signal(
            source="voice", timestamp="2026-02-15T10:00:00Z", raw_text="c",
        )

        assert endpoint.matches(alpha_signal) is True
        assert endpoint.matches(other_signal) is False
        assert endpoint.matches(no_init) is False

    def test_people_filter(self):
        endpoint = Endpoint(
            id="ben-channel",
            name="Ben's Channel",
            people=["Ben Booth", "Kevin"],
        )

        ben_signal = Signal(
            source="voice", timestamp="2026-02-15T10:00:00Z", raw_text="a",
            people=["Ben Booth"],
        )
        other_signal = Signal(
            source="voice", timestamp="2026-02-15T10:00:00Z", raw_text="b",
            people=["Alice"],
        )

        assert endpoint.matches(ben_signal) is True
        assert endpoint.matches(other_signal) is False


class TestTransitRecord:
    """Test TransitRecord dataclass."""

    def test_roundtrip(self):
        record = TransitRecord(
            signal_id="abc123",
            endpoint_id="test-endpoint",
            queued_at="2026-02-15T10:00:00Z",
            status="delivered",
        )

        d = record.to_dict()
        restored = TransitRecord.from_dict(d)

        assert restored.signal_id == record.signal_id
        assert restored.endpoint_id == record.endpoint_id
        assert restored.status == record.status


class TestRouter:
    """Test Router class."""

    @pytest.fixture
    def router_with_config(self, tmp_path):
        """Create a router with a test endpoints.yaml (dict-keyed format)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        endpoints_yaml = config_dir / "endpoints.yaml"
        endpoints_yaml.write_text("""\
all-signals:
  name: All Signals
  description: Catch-all for everything
  enabled: true
  interests:
    signal_types: all

blockers-only:
  name: Blocker Channel
  description: Only blocker signals
  enabled: true
  interests:
    signal_types:
      - blocker
    min_confidence: 0.7

disabled:
  name: Disabled Endpoint
  enabled: false
""")

        return Router(config_path=endpoints_yaml)

    def test_load_endpoints(self, router_with_config):
        endpoints = router_with_config.endpoints
        assert len(endpoints) == 3
        assert "all-signals" in endpoints
        assert endpoints["disabled"].enabled is False

    def test_route_signal_to_matching_endpoints(self, router_with_config):
        signal = Signal(
            source="voice",
            timestamp="2026-02-15T10:00:00Z",
            raw_text="Need help with thermal hydraulics code",
            signal_type="blocker",
            confidence=0.85,
        )

        routed = router_with_config.route([signal])

        assert "all-signals" in routed
        assert "blockers-only" in routed
        assert "disabled" not in routed

    def test_route_excludes_low_confidence(self, router_with_config):
        signal = Signal(
            source="voice",
            timestamp="2026-02-15T10:00:00Z",
            raw_text="Maybe a blocker?",
            signal_type="blocker",
            confidence=0.5,  # Below blockers-only threshold
        )

        routed = router_with_config.route([signal])

        assert "all-signals" in routed
        assert "blockers-only" not in routed  # Excluded due to confidence

    def test_queue_and_status(self, router_with_config):
        signals = [
            Signal(source="voice", timestamp="2026-02-15T10:00:00Z", raw_text="a"),
            Signal(source="voice", timestamp="2026-02-15T10:01:00Z", raw_text="b"),
        ]

        router_with_config.route(signals)
        status = router_with_config.status()

        assert status["status_counts"]["queued"] >= 2  # At least 2 signals queued

    def test_empty_config_fallback(self, tmp_path):
        """Router should work with no config file (empty endpoints)."""
        router = Router(config_path=tmp_path / "nonexistent.yaml")
        endpoints = router.endpoints

        assert endpoints == {}


class TestRouterIntegration:
    """Integration tests for router with file delivery."""

    def test_file_delivery(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        endpoints_yaml = config_dir / "endpoints.yaml"
        endpoints_yaml.write_text(f"""\
file-output:
  name: File Output
  enabled: true
  delivery:
    method: file
    path: {output_dir / 'signals.md'}
    format: markdown
""")

        router = Router(config_path=endpoints_yaml)

        signal = Signal(
            source="test",
            timestamp="2026-02-15T10:00:00Z",
            raw_text="Test signal content",
            detail="Test detail",
        )

        router.route([signal])
        router.deliver()

        # Verify file was created
        output_file = output_dir / "signals.md"
        assert output_file.exists()
