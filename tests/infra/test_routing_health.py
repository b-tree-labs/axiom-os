# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.infra.routing_health — Stage-2 classifier health.

Mirrors tests/rag/test_health.py.  The helper underpins
``axi config --status`` so the operator sees not just whether the LLM
endpoint is reachable but also whether the routing classifier's SLM
model is actually pulled and serving classifications.

Tolerance is the headline guarantee: a missing endpoint, a missing
model, or a network timeout MUST yield a well-formed
``ClassifierHealth`` rather than raise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from axiom.infra.routing_health import (
    ClassifierHealth,
    collect_classifier_health,
    render_classifier_health,
)

# ---------------------------------------------------------------------------
# ProbeRunner stubs
# ---------------------------------------------------------------------------


class _StubRunner:
    """Injectable ``ProbeRunner`` — returns canned responses per URL."""

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, *, timeout: float) -> dict | None:
        self.calls.append((url, timeout))
        outcome = self._responses.get(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # may be None to indicate unreachable


def _tags_response(model_names: list[str]) -> dict:
    """Mimic Ollama's GET /api/tags response shape."""
    return {"models": [{"name": n} for n in model_names]}


# ---------------------------------------------------------------------------
# collect_classifier_health — happy path
# ---------------------------------------------------------------------------


class TestCollectClassifierHealthHappy:
    def test_endpoint_reachable_and_model_loaded(self):
        runner = _StubRunner(
            {"http://localhost:11434/api/tags": _tags_response(["llama3.2:1b"])}
        )
        health = collect_classifier_health(
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
            runner=runner,
        )
        assert isinstance(health, ClassifierHealth)
        assert health.endpoint == "http://localhost:11434"
        assert health.endpoint_reachable is True
        assert health.configured_model == "llama3.2:1b"
        assert health.model_loaded is True
        assert health.model_loaded_check_error is None

    def test_model_match_tolerates_tag_variants(self):
        # Ollama may report ``llama3.2:1b`` or ``llama3.2:1b-instruct``; we
        # match on the base model name when the configured tag is missing
        # the instruct-suffix.
        runner = _StubRunner(
            {
                "http://localhost:11434/api/tags": _tags_response(
                    ["llama3.2:1b-instruct-q4_0"]
                )
            }
        )
        health = collect_classifier_health(
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
            runner=runner,
        )
        assert health.endpoint_reachable is True
        assert health.model_loaded is True


# ---------------------------------------------------------------------------
# collect_classifier_health — endpoint reachable, model missing
# ---------------------------------------------------------------------------


class TestCollectClassifierHealthMissingModel:
    def test_reachable_but_model_not_pulled(self):
        runner = _StubRunner(
            {"http://localhost:11434/api/tags": _tags_response(["mistral:7b"])}
        )
        health = collect_classifier_health(
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
            runner=runner,
        )
        assert health.endpoint_reachable is True
        assert health.model_loaded is False
        assert health.model_loaded_check_error is not None
        # Error message should hint at which model is missing.
        assert "llama3.2:1b" in health.model_loaded_check_error

    def test_reachable_but_no_models_at_all(self):
        runner = _StubRunner(
            {"http://localhost:11434/api/tags": _tags_response([])}
        )
        health = collect_classifier_health(
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
            runner=runner,
        )
        assert health.endpoint_reachable is True
        assert health.model_loaded is False
        assert health.model_loaded_check_error is not None


# ---------------------------------------------------------------------------
# collect_classifier_health — unreachable / timeout
# ---------------------------------------------------------------------------


class TestCollectClassifierHealthUnreachable:
    def test_unreachable_endpoint(self):
        runner = _StubRunner({"http://localhost:11434/api/tags": None})
        health = collect_classifier_health(
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
            runner=runner,
        )
        assert health.endpoint_reachable is False
        assert health.model_loaded is False
        assert health.model_loaded_check_error is not None
        # When the endpoint is unreachable we couldn't check the model;
        # the error should say so.
        assert (
            "unreachable" in health.model_loaded_check_error.lower()
            or "could not" in health.model_loaded_check_error.lower()
        )

    def test_timeout_surfaces_in_error(self):
        runner = _StubRunner(
            {"http://localhost:11434/api/tags": TimeoutError("read timed out")}
        )
        health = collect_classifier_health(
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
            runner=runner,
        )
        assert health.endpoint_reachable is False
        assert health.model_loaded is False
        assert health.model_loaded_check_error is not None
        assert "timeout" in health.model_loaded_check_error.lower()

    def test_arbitrary_exception_swallowed(self):
        runner = _StubRunner(
            {"http://localhost:11434/api/tags": ValueError("boom")}
        )
        # Must not raise — surface via model_loaded_check_error.
        health = collect_classifier_health(
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
            runner=runner,
        )
        assert health.endpoint_reachable is False
        assert health.model_loaded is False
        assert health.model_loaded_check_error is not None


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestCollectClassifierHealthDefaults:
    def test_defaults_endpoint_and_model(self, monkeypatch):
        # When endpoint=None and model=None, fall back to settings or built-in
        # defaults.  Pin the SettingsStore to a known value so we don't depend
        # on the dev machine's settings.toml.
        monkeypatch.setattr(
            "axiom.infra.routing_health._read_settings",
            lambda: {
                "routing.ollama_base": "http://example:9999",
                "routing.ollama_model": "phi3:mini",
            },
        )
        runner = _StubRunner(
            {"http://example:9999/api/tags": _tags_response(["phi3:mini"])}
        )
        health = collect_classifier_health(runner=runner)
        assert health.endpoint == "http://example:9999"
        assert health.configured_model == "phi3:mini"
        assert health.endpoint_reachable is True
        assert health.model_loaded is True

    def test_settings_store_failure_uses_built_in_defaults(self, monkeypatch):
        # If the SettingsStore blows up, we still produce a well-formed
        # ClassifierHealth using the documented built-in defaults.
        monkeypatch.setattr(
            "axiom.infra.routing_health._read_settings",
            lambda: (_ for _ in ()).throw(RuntimeError("settings missing")),
        )
        runner = _StubRunner(
            {"http://localhost:11434/api/tags": _tags_response([])}
        )
        health = collect_classifier_health(runner=runner)
        assert health.endpoint == "http://localhost:11434"
        assert health.configured_model == "llama3.2:1b"


# ---------------------------------------------------------------------------
# Recent classification metrics from audit log
# ---------------------------------------------------------------------------


class TestClassifierHealthAuditMetrics:
    def test_no_audit_log_yields_none(self, tmp_path, monkeypatch):
        # No audit dir at all → last_classification_at None, p50_latency None.
        monkeypatch.setattr(
            "axiom.infra.routing_health._audit_log_dir",
            lambda: tmp_path / "no-such-dir",
        )
        runner = _StubRunner(
            {"http://localhost:11434/api/tags": _tags_response(["llama3.2:1b"])}
        )
        health = collect_classifier_health(
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
            runner=runner,
        )
        assert health.last_classification_at is None
        assert health.p50_latency_ms is None

    def test_recent_classification_events_surface(self, tmp_path, monkeypatch):
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        events_path = audit_dir / "classification_events.jsonl"

        recent = datetime.now(UTC) - timedelta(minutes=5)
        rows = [
            {
                "ts": (recent - timedelta(minutes=i)).isoformat(),
                "classifier": "ollama",
                "latency_ms": 300 + i * 20,
            }
            for i in range(5)
        ]
        events_path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )

        monkeypatch.setattr(
            "axiom.infra.routing_health._audit_log_dir", lambda: audit_dir
        )
        runner = _StubRunner(
            {"http://localhost:11434/api/tags": _tags_response(["llama3.2:1b"])}
        )
        health = collect_classifier_health(
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
            runner=runner,
        )
        assert health.last_classification_at is not None
        assert "T" in health.last_classification_at  # ISO-8601 shape
        # Median of 300,320,340,360,380 = 340
        assert health.p50_latency_ms == 340

    def test_corrupt_audit_file_does_not_raise(self, tmp_path, monkeypatch):
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        (audit_dir / "classification_events.jsonl").write_text(
            "not-json\n{also_bad\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            "axiom.infra.routing_health._audit_log_dir", lambda: audit_dir
        )
        runner = _StubRunner(
            {"http://localhost:11434/api/tags": _tags_response(["llama3.2:1b"])}
        )
        health = collect_classifier_health(
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
            runner=runner,
        )
        # Tolerant — corrupt rows skipped, no crash.
        assert health.last_classification_at is None
        assert health.p50_latency_ms is None


# ---------------------------------------------------------------------------
# render_classifier_health output
# ---------------------------------------------------------------------------


class TestRenderClassifierHealth:
    def test_render_all_green(self, capsys):
        h = ClassifierHealth(
            endpoint="http://localhost:11434",
            endpoint_reachable=True,
            configured_model="llama3.2:1b",
            model_loaded=True,
            model_loaded_check_error=None,
            last_classification_at="2026-04-28T19:34:12+00:00",
            p50_latency_ms=340,
        )
        render_classifier_health(h)
        out = capsys.readouterr().out
        assert "Routing Classifier" in out
        assert "http://localhost:11434" in out
        assert "llama3.2:1b" in out
        # No actionable hint when everything's green.
        assert "ollama pull" not in out
        # Latency printed.
        assert "340" in out

    def test_render_missing_model_shows_pull_hint(self, capsys):
        h = ClassifierHealth(
            endpoint="http://localhost:11434",
            endpoint_reachable=True,
            configured_model="llama3.2:1b",
            model_loaded=False,
            model_loaded_check_error="model 'llama3.2:1b' not found in /api/tags",
            last_classification_at=None,
            p50_latency_ms=None,
        )
        render_classifier_health(h)
        out = capsys.readouterr().out
        assert "Routing Classifier" in out
        # The actionable hint is the headline ask for this whole feature.
        assert "ollama pull llama3.2:1b" in out
        assert "never" in out.lower() or "n/a" in out.lower()

    def test_render_unreachable_endpoint(self, capsys):
        h = ClassifierHealth(
            endpoint="http://localhost:11434",
            endpoint_reachable=False,
            configured_model="llama3.2:1b",
            model_loaded=False,
            model_loaded_check_error="endpoint unreachable",
            last_classification_at=None,
            p50_latency_ms=None,
        )
        render_classifier_health(h)
        out = capsys.readouterr().out
        assert "Routing Classifier" in out
        # Unreachable endpoints should NOT show the pull hint — pulling a
        # model when the daemon is down is the wrong action.
        assert "ollama pull" not in out

    def test_render_is_domain_agnostic(self, capsys):
        # Per the axiom-domain-agnostic rule: the renderer must never leak
        # domain-specific terms or internal host/site names.
        h = ClassifierHealth(
            endpoint="http://localhost:11434",
            endpoint_reachable=True,
            configured_model="llama3.2:1b",
            model_loaded=True,
            model_loaded_check_error=None,
            last_classification_at="2026-04-28T19:34:12+00:00",
            p50_latency_ms=340,
        )
        render_classifier_health(h)
        out = capsys.readouterr().out.lower()
        for forbidden in ("nuclear", "netl", "rascal", "reactor"):
            assert forbidden not in out


# ---------------------------------------------------------------------------
# axi config --status integration
# ---------------------------------------------------------------------------


class TestConfigStatusIntegration:
    def test_show_status_includes_classifier_section(
        self, tmp_path, capsys, monkeypatch
    ):
        from axiom.setup.wizard import SetupWizard

        wizard = SetupWizard(root=tmp_path)
        sentinel = ClassifierHealth(
            endpoint="http://localhost:11434",
            endpoint_reachable=True,
            configured_model="llama3.2:1b",
            model_loaded=False,
            model_loaded_check_error="model 'llama3.2:1b' not found in /api/tags",
            last_classification_at=None,
            p50_latency_ms=None,
        )
        monkeypatch.setattr(
            "axiom.setup.wizard.collect_classifier_health",
            lambda *a, **kw: sentinel,
        )
        wizard.show_status()
        out = capsys.readouterr().out
        assert "Configuration Status" in out
        assert "Routing Classifier" in out
        assert "llama3.2:1b" in out
        # Surfaces the actionable hint operators have been missing.
        assert "ollama pull llama3.2:1b" in out
