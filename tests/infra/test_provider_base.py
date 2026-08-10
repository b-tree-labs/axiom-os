# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for axiom.infra.provider_base.

Run:
    pytest tests/infra/test_provider_base.py -v
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pytest

# ---------------------------------------------------------------------------
# ProviderIdentityMixin — pure mixin, works with @dataclass
# ---------------------------------------------------------------------------

class TestProviderIdentityMixin:

    def _make_p(self, uid="", fingerprint_fields=()):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _log_prefix = "test_provider"
            _fingerprint_fields = fingerprint_fields
            def __init__(self, cfg=None):
                self.name = "my-provider"
                self._compute_identity(cfg or {})

        return P({"uid": uid} if uid else {})

    def test_config_hash_is_8_chars(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _log_prefix = "test_provider"
            _fingerprint_fields = ("endpoint", "model")
            def __init__(self):
                self.name = "my-provider"
                self._compute_identity({"endpoint": "http://x", "model": "gpt4"})

        p = P()
        assert len(p.config_hash) == 8

    def test_config_hash_stable_for_same_fields(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _fingerprint_fields = ("endpoint", "model")
            def __init__(self, cfg):
                self.name = "p"
                self._compute_identity(cfg)

        cfg = {"endpoint": "http://x", "model": "gpt4"}
        assert P(cfg).config_hash == P(cfg).config_hash

    def test_config_hash_changes_when_fingerprint_fields_change(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _fingerprint_fields = ("endpoint",)
            def __init__(self, ep):
                self.name = "p"
                self._compute_identity({"endpoint": ep})

        assert P("http://a").config_hash != P("http://b").config_hash

    def test_instance_id_unique_per_instantiation(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _fingerprint_fields = ()
            def __init__(self):
                self.name = "p"
                self._compute_identity({})

        assert P().instance_id != P().instance_id

    def test_instance_id_is_12_chars(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _fingerprint_fields = ()
            def __init__(self):
                self.name = "p"
                self._compute_identity({})

        assert len(P().instance_id) == 12

    def test_uid_taken_from_config_when_present(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _fingerprint_fields = ()
            def __init__(self):
                self.name = "p"
                self._compute_identity({"uid": "explicit-uid-abc"})

        p = P()
        assert p.uid == "explicit-uid-abc"

    def test_uid_auto_generated_when_absent(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _fingerprint_fields = ()
            def __init__(self):
                self.name = "p"
                self._compute_identity({})

        p = P()
        assert p.uid  # non-empty
        assert len(p.uid) >= 32  # UUID format

    def test_uid_stable_when_in_config(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _fingerprint_fields = ()
            def __init__(self):
                self.name = "p"
                self._compute_identity({"uid": "my-stable-uid"})

        assert P().uid == P().uid == "my-stable-uid"

    def test_uid_unique_when_auto_generated(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _fingerprint_fields = ()
            def __init__(self):
                self.name = "p"
                self._compute_identity({})

        assert P().uid != P().uid

    def test_compute_identity_returns_true_when_uid_generated(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _fingerprint_fields = ()
            def __init__(self):
                self.name = "p"
                self.generated = self._compute_identity({})

        assert P().generated is True

    def test_compute_identity_returns_false_when_uid_in_config(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _fingerprint_fields = ()
            def __init__(self):
                self.name = "p"
                self.generated = self._compute_identity({"uid": "given-uid"})

        assert P().generated is False

    def test_identity_dict_uses_log_prefix(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _log_prefix = "llm_provider"
            _fingerprint_fields = ()
            def __init__(self):
                self.name = "llm-private"
                self._compute_identity({"uid": "fixed-uid"})

        p = P()
        identity = p.identity
        assert identity["llm_provider"] == "llm-private"
        assert identity["llm_provider_uid"] == "fixed-uid"
        assert "llm_provider_config_hash" in identity
        assert "llm_provider_instance" in identity

    def test_identity_keys_change_with_log_prefix(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class Sink(ProviderIdentityMixin):
            _log_prefix = "log_sink"
            _fingerprint_fields = ()
            def __init__(self):
                self.name = "gcp"
                self._compute_identity({})

        identity = Sink().identity
        assert "log_sink" in identity
        assert "log_sink_uid" in identity
        assert "log_sink_config_hash" in identity
        assert "log_sink_instance" in identity

    def test_mixin_works_with_dataclass(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        @dataclass
        class LLMProv(ProviderIdentityMixin):
            name: str
            uid: str = ""
            endpoint: str = ""
            model: str = ""
            _log_prefix: str = field(default="llm_provider", init=False, repr=False)
            _fingerprint_fields: tuple = field(
                default=("endpoint", "model"), init=False, repr=False
            )
            config_hash: str = field(default="", init=False)
            instance_id: str = field(default="", init=False)

            def __post_init__(self):
                self._compute_identity({"uid": self.uid, "endpoint": self.endpoint, "model": self.model})

        p = LLMProv(name="llm-private", uid="abc-123", endpoint="http://x", model="qwen3")
        assert p.config_hash
        assert p.instance_id
        assert p.uid == "abc-123"
        assert p.identity["llm_provider"] == "llm-private"
        assert p.identity["llm_provider_uid"] == "abc-123"

    def test_fingerprint_fields_not_in_config_use_empty_string(self):
        from axiom.infra.provider_base import ProviderIdentityMixin

        class P(ProviderIdentityMixin):
            _fingerprint_fields = ("missing_key",)
            def __init__(self):
                self.name = "p"
                self._compute_identity({})

        # Should not raise — missing fields treated as ""
        p = P()
        assert len(p.config_hash) == 8


# ---------------------------------------------------------------------------
# ProviderBase — full base for non-dataclass providers
# ---------------------------------------------------------------------------

class TestProviderBase:

    def _make_concrete(self, **cls_attrs):
        from axiom.infra.provider_base import ProviderBase

        class Concrete(ProviderBase):
            _log_prefix = cls_attrs.get("_log_prefix", "test_provider")
            _fingerprint_fields = cls_attrs.get("_fingerprint_fields", ("endpoint",))
            _required_config = cls_attrs.get("_required_config", ())

        return Concrete

    def test_name_from_config(self):
        Concrete = self._make_concrete()
        p = Concrete({"name": "my-svc", "uid": "u1", "endpoint": "http://x"})
        assert p.name == "my-svc"

    def test_name_defaults_to_class_name_lower(self):
        Concrete = self._make_concrete()
        p = Concrete({"endpoint": "http://x"})
        assert p.name == "concrete"

    def test_required_config_missing_raises_value_error(self):
        Concrete = self._make_concrete(_required_config=("endpoint", "api_key"))
        with pytest.raises(ValueError, match="api_key"):
            Concrete({"endpoint": "http://x"})  # api_key missing

    def test_required_config_present_does_not_raise(self):
        Concrete = self._make_concrete(_required_config=("endpoint",))
        Concrete({"endpoint": "http://x"})  # should not raise

    def test_identity_computed_on_init(self):
        Concrete = self._make_concrete()
        p = Concrete({"name": "svc", "uid": "u1", "endpoint": "http://x"})
        assert len(p.config_hash) == 8
        assert len(p.instance_id) == 12

    def test_uid_from_config_stored_on_instance(self):
        Concrete = self._make_concrete()
        p = Concrete({"name": "svc", "uid": "my-persistent-uid", "endpoint": "http://x"})
        assert p.uid == "my-persistent-uid"

    def test_uid_auto_generated_when_absent(self):
        Concrete = self._make_concrete()
        p = Concrete({"name": "svc", "endpoint": "http://x"})
        assert p.uid  # non-empty

    def test_uid_warning_emitted_when_absent(self, caplog):
        Concrete = self._make_concrete()
        with caplog.at_level(logging.WARNING):
            Concrete({"name": "my-svc", "endpoint": "http://x"})
        assert "uid" in caplog.text.lower()

    def test_uid_no_warning_when_present(self, caplog):
        Concrete = self._make_concrete()
        with caplog.at_level(logging.WARNING):
            Concrete({"name": "svc", "uid": "given-uid", "endpoint": "http://x"})
        assert "uid" not in caplog.text or "given-uid" not in caplog.text

    def test_uid_warning_contains_generated_value(self, caplog):
        Concrete = self._make_concrete()
        with caplog.at_level(logging.WARNING):
            p = Concrete({"name": "my-svc", "endpoint": "http://x"})
        assert p.uid in caplog.text

    def test_available_returns_true_by_default(self):
        Concrete = self._make_concrete()
        p = Concrete({"name": "svc", "uid": "u1", "endpoint": "http://x"})
        assert p.available() is True

    def test_available_can_be_overridden(self):
        from axiom.infra.provider_base import ProviderBase

        class AlwaysDown(ProviderBase):
            _log_prefix = "test"
            _fingerprint_fields = ()
            def available(self):
                return False

        p = AlwaysDown({})
        assert p.available() is False

    def test_describe_returns_nonempty_string(self):
        Concrete = self._make_concrete()
        p = Concrete({"name": "svc", "uid": "u1", "endpoint": "http://x"})
        desc = p.describe()
        assert isinstance(desc, str)
        assert len(desc) > 0

    def test_describe_includes_name_and_hash(self):
        Concrete = self._make_concrete()
        p = Concrete({"name": "my-svc", "uid": "u1", "endpoint": "http://x"})
        desc = p.describe()
        assert "my-svc" in desc
        assert p.config_hash in desc

    def test_logger_is_standard_python_logger(self):
        Concrete = self._make_concrete()
        p = Concrete({"name": "svc", "endpoint": "http://x"})
        assert isinstance(p._logger, logging.Logger)

    def test_handles_sensitive_data_false_by_default(self):
        Concrete = self._make_concrete()
        p = Concrete({})
        assert p.handles_sensitive_data is False

    def test_handles_sensitive_data_can_be_set_on_class(self):
        from axiom.infra.provider_base import ProviderBase

        class SecureStore(ProviderBase):
            _log_prefix = "storage_provider"
            _fingerprint_fields = ()
            handles_sensitive_data = True

        p = SecureStore({})
        assert p.handles_sensitive_data is True

    def test_config_stored_on_instance(self):
        Concrete = self._make_concrete()
        cfg = {"name": "svc", "endpoint": "http://x"}
        p = Concrete(cfg)
        assert p._config is cfg


class _nullctx:
    """Minimal no-op context manager."""
    def __enter__(self): return self
    def __exit__(self, *_): return False


# ---------------------------------------------------------------------------
# Integration: LLMProvider uses ProviderIdentityMixin
# ---------------------------------------------------------------------------

class TestLLMProviderIdentity:

    def test_llmprovider_has_identity_property(self):
        from axiom.infra.gateway import LLMProvider
        p = LLMProvider(name="llm-private", uid="fixed-uid", endpoint="http://x/v1", model="qwen3")
        identity = p.identity
        assert identity["llm_provider"] == "llm-private"
        assert identity["llm_provider_uid"] == "fixed-uid"
        assert "llm_provider_config_hash" in identity
        assert "llm_provider_instance" in identity

    def test_llmprovider_config_hash_stable(self):
        from axiom.infra.gateway import LLMProvider
        p1 = LLMProvider(name="p", uid="u", endpoint="http://x/v1", model="m", routing_tier="public")
        p2 = LLMProvider(name="p", uid="u", endpoint="http://x/v1", model="m", routing_tier="public")
        assert p1.config_hash == p2.config_hash

    def test_llmprovider_instance_id_unique(self):
        from axiom.infra.gateway import LLMProvider
        p1 = LLMProvider(name="p", uid="u", endpoint="http://x/v1", model="m")
        p2 = LLMProvider(name="p", uid="u", endpoint="http://x/v1", model="m")
        assert p1.instance_id != p2.instance_id

    def test_llmprovider_uid_stable_when_provided(self):
        from axiom.infra.gateway import LLMProvider
        p1 = LLMProvider(name="p", uid="my-uid", endpoint="http://x/v1", model="m")
        p2 = LLMProvider(name="p", uid="my-uid", endpoint="http://x/v1", model="m")
        assert p1.uid == p2.uid == "my-uid"

    def test_llmprovider_uses_mixin(self):
        from axiom.infra.gateway import LLMProvider
        from axiom.infra.provider_base import ProviderIdentityMixin
        assert issubclass(LLMProvider, ProviderIdentityMixin)


# ---------------------------------------------------------------------------
# Integration: LogSinkBase uses ProviderBase
# ---------------------------------------------------------------------------

class TestLogSinkBaseIdentity:

    def test_log_sink_has_identity(self):
        import os
        import tempfile

        from axiom.infra.log_sinks import FileSink
        with tempfile.TemporaryDirectory() as d:
            sink = FileSink({"name": "my-file-sink", "uid": "sink-uid-1", "path": os.path.join(d, "out.jsonl")})
            identity = sink.identity
            assert identity["log_sink"] == "my-file-sink"
            assert identity["log_sink_uid"] == "sink-uid-1"
            assert "log_sink_config_hash" in identity

    def test_log_sink_uses_provider_base(self):
        from axiom.infra.log_sinks import LogSinkBase
        from axiom.infra.provider_base import ProviderBase
        assert issubclass(LogSinkBase, ProviderBase)

    def test_null_sink_has_describe(self):
        from axiom.infra.log_sinks import NullSink
        sink = NullSink({"name": "dev-null", "uid": "null-uid"})
        assert "dev-null" in sink.describe()

    def test_file_sink_handles_sensitive_data_false(self):
        import os
        import tempfile

        from axiom.infra.log_sinks import FileSink
        with tempfile.TemporaryDirectory() as d:
            sink = FileSink({"path": os.path.join(d, "out.jsonl")})
            assert sink.handles_sensitive_data is False


# ---------------------------------------------------------------------------
# ensure_provider_uids — TOML uid back-fill helper
# ---------------------------------------------------------------------------

class TestEnsureProviderUids:

    def _write_toml(self, path, content: str):
        path.write_text(content, encoding="utf-8")

    def test_returns_false_when_file_missing(self, tmp_path):
        from axiom.infra.provider_base import ensure_provider_uids
        result = ensure_provider_uids(tmp_path / "nonexistent.toml", "log.sinks")
        assert result is False

    def test_returns_false_when_all_uids_present(self, tmp_path):
        from axiom.infra.provider_base import ensure_provider_uids
        f = tmp_path / "logging.toml"
        self._write_toml(f, '[[log.sinks]]\ntype = "file"\nuid = "abc-123"\n')
        assert ensure_provider_uids(f, "log.sinks") is False

    def test_returns_true_and_writes_missing_uid(self, tmp_path):
        from axiom.infra.provider_base import ensure_provider_uids
        f = tmp_path / "logging.toml"
        self._write_toml(f, '[[log.sinks]]\ntype = "file"\n')
        result = ensure_provider_uids(f, "log.sinks")
        assert result is True
        text = f.read_text()
        assert "uid" in text

    def test_written_uid_is_valid_uuid(self, tmp_path):
        import uuid

        from axiom.infra.provider_base import ensure_provider_uids
        f = tmp_path / "logging.toml"
        self._write_toml(f, '[[log.sinks]]\ntype = "file"\n')
        ensure_provider_uids(f, "log.sinks")
        import tomlkit
        doc = tomlkit.parse(f.read_text())
        written_uid = doc["log"]["sinks"][0]["uid"]
        uuid.UUID(written_uid)  # raises if invalid

    def test_existing_uid_not_overwritten(self, tmp_path):
        from axiom.infra.provider_base import ensure_provider_uids
        f = tmp_path / "logging.toml"
        self._write_toml(f, '[[log.sinks]]\ntype = "file"\nuid = "keep-me"\n')
        ensure_provider_uids(f, "log.sinks")
        import tomlkit
        doc = tomlkit.parse(f.read_text())
        assert doc["log"]["sinks"][0]["uid"] == "keep-me"

    def test_partial_fill_only_missing_entries(self, tmp_path):
        from axiom.infra.provider_base import ensure_provider_uids
        f = tmp_path / "logging.toml"
        self._write_toml(f, (
            '[[log.sinks]]\ntype = "file"\nuid = "existing"\n\n'
            '[[log.sinks]]\ntype = "null"\n'
        ))
        ensure_provider_uids(f, "log.sinks")
        import tomlkit
        doc = tomlkit.parse(f.read_text())
        assert doc["log"]["sinks"][0]["uid"] == "existing"
        assert doc["log"]["sinks"][1]["uid"]  # newly generated

    def test_preserves_comments_on_write(self, tmp_path):
        from axiom.infra.provider_base import ensure_provider_uids
        f = tmp_path / "logging.toml"
        self._write_toml(f, '# my comment\n[[log.sinks]]\ntype = "file"\n')
        ensure_provider_uids(f, "log.sinks")
        assert "# my comment" in f.read_text()

    def test_returns_false_for_missing_table_key(self, tmp_path):
        from axiom.infra.provider_base import ensure_provider_uids
        f = tmp_path / "logging.toml"
        self._write_toml(f, '[other]\nkey = "val"\n')
        result = ensure_provider_uids(f, "log.sinks")
        assert result is False

    def test_duplicate_uid_logged_as_error(self, tmp_path, caplog):
        from axiom.infra.provider_base import ensure_provider_uids
        f = tmp_path / "logging.toml"
        self._write_toml(f, (
            '[[log.sinks]]\nname = "sink-a"\nuid = "same-uid"\n\n'
            '[[log.sinks]]\nname = "sink-b"\nuid = "same-uid"\n'
        ))
        with caplog.at_level(logging.ERROR):
            ensure_provider_uids(f, "log.sinks")
        assert "same-uid" in caplog.text
        assert "sink-b" in caplog.text

    def test_unique_uids_produce_no_error(self, tmp_path, caplog):
        from axiom.infra.provider_base import ensure_provider_uids
        f = tmp_path / "logging.toml"
        self._write_toml(f, (
            '[[log.sinks]]\nname = "a"\nuid = "uid-1"\n\n'
            '[[log.sinks]]\nname = "b"\nuid = "uid-2"\n'
        ))
        with caplog.at_level(logging.ERROR):
            ensure_provider_uids(f, "log.sinks")
        assert "Duplicate" not in caplog.text
