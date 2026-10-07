# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axiom.infra.config`` — schema + values + locks + watcher.

Covers:
- Schema registration + validation (type checking, re-registration rules)
- Value get/write/locks
- Subscriber pattern (observe / unsub)
- Filesystem watcher end-to-end (polling backend for determinism)
- Lock predicate behavior (the keystore session owns crypto; we own the
  predicate)
- AEOS §2.13 normative property: get_value never returns a cached
  snapshot that's stale-relative-to-the-registry
"""

from __future__ import annotations

import time

import pytest

from axiom.infra.config import (
    LockedConfigError,
    SchemaError,
    default_config_dir,
    get_registry,
    get_value,
    lock,
    lock_status,
    observe,
    register_schema,
    start_watching,
    stop_watching,
    unlock,
    write_value,
)
from axiom.infra.config import observer as observer_mod
from axiom.infra.config import registry as registry_mod
from axiom.infra.config.watcher import PollingWatcher, load_config_file


@pytest.fixture(autouse=True)
def _clean_singletons():
    """Each test gets a fresh registry + observer."""
    registry_mod.reset_for_testing()
    observer_mod.reset_for_testing()
    stop_watching()
    yield
    stop_watching()
    registry_mod.reset_for_testing()
    observer_mod.reset_for_testing()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchemaRegistration:
    def test_register_simple_type(self):
        register_schema("expman", {"sla_hours": int})
        assert get_value("expman.sla_hours", default=24) == 24

    def test_register_dict_form_sets_default(self):
        register_schema(
            "expman",
            {"sla_hours": {"type": int, "default": 48}},
        )
        # The field's declared default is what get_value returns when
        # the value hasn't been explicitly written.
        assert get_value("expman.sla_hours") == 48

    def test_unknown_key_returns_default(self):
        assert get_value("expman.unknown", default="fallback") == "fallback"

    def test_re_registration_compatible_ok(self):
        register_schema("expman", {"sla_hours": int})
        register_schema("expman", {"sla_hours": int})

    def test_re_registration_incompatible_raises(self):
        register_schema("expman", {"sla_hours": int})
        with pytest.raises(SchemaError):
            register_schema("expman", {"sla_hours": str})

    def test_write_wrong_type_rejected(self):
        register_schema("expman", {"sla_hours": int})
        with pytest.raises(SchemaError):
            write_value("expman.sla_hours", "not-an-int")

    def test_write_unregistered_key_rejected(self):
        with pytest.raises(SchemaError):
            write_value("nothing.here", 1)


# ---------------------------------------------------------------------------
# Read / write / observers
# ---------------------------------------------------------------------------


class TestReadWriteObserve:
    def test_get_value_after_write(self):
        register_schema("expman", {"sla_hours": int})
        write_value("expman.sla_hours", 48, actor="@austin:example-org")
        assert get_value("expman.sla_hours") == 48

    def test_get_value_never_caches_a_stale_snapshot(self):
        """AEOS §2.13: callers must see the registry's current value."""
        register_schema("expman", {"sla_hours": int})
        write_value("expman.sla_hours", 24, actor="@austin:example-org")
        # Simulate another writer (file watcher, peer agent, etc.) mutating
        # the value between the first read and the second.
        first = get_value("expman.sla_hours")
        write_value("expman.sla_hours", 48, actor="@compliance:example-org")
        second = get_value("expman.sla_hours")
        assert (first, second) == (24, 48)

    def test_observer_fires_on_change(self):
        register_schema("expman", {"sla_hours": int})
        seen: list[tuple] = []
        observe(
            "expman.sla_hours",
            lambda old, new, src: seen.append((old, new, src)),
        )
        write_value("expman.sla_hours", 24, actor="@a", source="test")
        write_value("expman.sla_hours", 48, actor="@a", source="test")
        assert seen == [(None, 24, "test"), (24, 48, "test")]

    def test_no_observer_fire_for_unchanged_value(self):
        register_schema("expman", {"sla_hours": int})
        seen: list[tuple] = []
        observe(
            "expman.sla_hours",
            lambda old, new, src: seen.append((old, new, src)),
        )
        write_value("expman.sla_hours", 24)
        write_value("expman.sla_hours", 24)  # same value
        assert len(seen) == 1

    def test_observer_failure_does_not_block_other_observers(self):
        register_schema("expman", {"sla_hours": int})
        seen: list[tuple] = []

        def crashing(old, new, src):
            raise RuntimeError("kaboom")

        observe("expman.sla_hours", crashing)
        observe(
            "expman.sla_hours",
            lambda old, new, src: seen.append((old, new)),
        )
        write_value("expman.sla_hours", 7)
        assert seen == [(None, 7)]

    def test_unsubscribe_stops_callbacks(self):
        register_schema("expman", {"sla_hours": int})
        seen: list[tuple] = []
        unsub = observe(
            "expman.sla_hours",
            lambda old, new, src: seen.append((old, new)),
        )
        write_value("expman.sla_hours", 1)
        unsub()
        write_value("expman.sla_hours", 2)
        assert seen == [(None, 1)]


# ---------------------------------------------------------------------------
# Locks — the compose-point with the keystore session
# ---------------------------------------------------------------------------


class TestLocks:
    def test_locked_key_rejects_write(self):
        register_schema("expman", {"sla_hours": int})
        write_value("expman.sla_hours", 24)
        lock(
            "expman.sla_hours",
            locked_by="@compliance:example-org",
            reason="change-control",
        )
        with pytest.raises(LockedConfigError) as ei:
            write_value("expman.sla_hours", 99)
        assert ei.value.key == "expman.sla_hours"
        assert ei.value.lock.locked_by == "@compliance:example-org"

    def test_locked_key_accepts_write_with_override_capability(self):
        register_schema("expman", {"sla_hours": int})
        lock(
            "expman.sla_hours",
            locked_by="@compliance:example-org",
            reason="change-control",
        )
        # The presence of any non-None override_capability satisfies
        # *this* module — the keystore session is responsible for
        # verifying the capability actually grants the override.
        write_value(
            "expman.sla_hours",
            99,
            override_capability=object(),
        )
        assert get_value("expman.sla_hours") == 99

    def test_lockable_false_rejects_lock(self):
        register_schema(
            "expman",
            {"sla_hours": {"type": int, "lockable": False}},
        )
        with pytest.raises(SchemaError):
            lock("expman.sla_hours", locked_by="@x", reason="no")

    def test_lock_status_reports_lock(self):
        register_schema("expman", {"sla_hours": int})
        assert lock_status("expman.sla_hours") is None
        lock(
            "expman.sla_hours",
            locked_by="@compliance:example-org",
            reason="change-control",
            override_capability_pattern="compliance.override_lock",
        )
        ls = lock_status("expman.sla_hours")
        assert ls is not None
        assert ls.locked_by == "@compliance:example-org"
        assert ls.override_capability_pattern == "compliance.override_lock"

    def test_unlock_removes_lock(self):
        register_schema("expman", {"sla_hours": int})
        lock("expman.sla_hours", locked_by="@x", reason="r")
        unlock("expman.sla_hours", override_capability=object())
        assert lock_status("expman.sla_hours") is None
        # Writes work again.
        write_value("expman.sla_hours", 7)
        assert get_value("expman.sla_hours") == 7


# ---------------------------------------------------------------------------
# load_config_file — flattening rule
# ---------------------------------------------------------------------------


class TestLoadConfigFile:
    def test_flattens_nested_toml(self, tmp_path):
        p = tmp_path / "x.toml"
        p.write_text(
            "[expman]\n"
            "sla_hours = 24\n"
            'compliance = "@compliance:example-org"\n'
            "\n"
            "[expman.advanced]\n"
            "auto_dispose_days = 30\n"
        )
        flat = load_config_file(p)
        assert flat["expman.sla_hours"] == 24
        assert flat["expman.compliance"] == "@compliance:example-org"
        assert flat["expman.advanced.auto_dispose_days"] == 30

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_config_file(tmp_path / "no.toml") == {}


# ---------------------------------------------------------------------------
# Filesystem watcher — end-to-end with the polling backend
# ---------------------------------------------------------------------------


class TestFilesystemWatcher:
    """Drive the PollingWatcher's ``poll_once`` directly to dodge the
    started-thread race; the start/stop path is tested in
    test_watcher_thread below.
    """

    def _build_watcher(self, tmp_path):

        def _apply(path, values):
            get_registry().load_dict(
                values, actor="(file)", source=f"file:{path}"
            )

        return PollingWatcher(directory=tmp_path, apply_fn=_apply)

    def test_polling_watcher_picks_up_initial_write(self, tmp_path):
        register_schema("expman", {"sla_hours": int})
        seen: list[tuple] = []
        observe(
            "expman.sla_hours",
            lambda old, new, src: seen.append((old, new, src)),
        )

        watcher = self._build_watcher(tmp_path)
        cfg = tmp_path / "expman.toml"
        cfg.write_text("[expman]\nsla_hours = 48\n")
        watcher.poll_once()

        assert len(seen) == 1
        assert seen[0][:2] == (None, 48)
        assert seen[0][2].startswith("file:")

    def test_polling_watcher_picks_up_subsequent_change(self, tmp_path):
        register_schema("expman", {"sla_hours": int})
        seen: list[tuple] = []
        observe(
            "expman.sla_hours",
            lambda old, new, src: seen.append((old, new)),
        )
        cfg = tmp_path / "expman.toml"
        cfg.write_text("[expman]\nsla_hours = 24\n")
        watcher = self._build_watcher(tmp_path)
        watcher.poll_once()

        assert seen[-1] == (None, 24)
        time.sleep(0.02)
        cfg.write_text("[expman]\nsla_hours = 48\n")
        import os

        st = cfg.stat()
        os.utime(cfg, (st.st_atime, st.st_mtime + 1.0))
        watcher.poll_once()
        assert seen[-1] == (24, 48)

    def test_watcher_validation_failure_does_not_apply(self, tmp_path):
        register_schema("expman", {"sla_hours": int})
        seen: list[tuple] = []
        observe(
            "expman.sla_hours",
            lambda old, new, src: seen.append((old, new)),
        )
        cfg = tmp_path / "expman.toml"
        watcher = self._build_watcher(tmp_path)

        # First a valid write — should apply.
        cfg.write_text("[expman]\nsla_hours = 24\n")
        watcher.poll_once()
        assert seen == [(None, 24)]

        # Now a type-mismatched write — should NOT apply.
        time.sleep(0.02)
        cfg.write_text('[expman]\nsla_hours = "twenty-four"\n')
        import os

        st = cfg.stat()
        os.utime(cfg, (st.st_atime, st.st_mtime + 1.0))
        watcher.poll_once()
        assert seen == [(None, 24)]
        assert get_value("expman.sla_hours") == 24


class TestWatcherThreadLifecycle:
    """Cover start/stop without timing-fragile assertions.

    The watcher's poll logic is covered deterministically in
    ``TestFilesystemWatcher`` above by calling ``poll_once`` directly.
    Here we only verify start/stop succeed and the watcher object is
    of the expected backend type.
    """

    def test_start_returns_watcher_and_is_idempotent(self, tmp_path):
        watcher_a = start_watching(tmp_path, prefer_polling=True)
        watcher_b = start_watching(tmp_path, prefer_polling=True)
        assert isinstance(watcher_a, PollingWatcher)
        assert watcher_a is watcher_b
        stop_watching()

    def test_stop_is_idempotent(self, tmp_path):
        start_watching(tmp_path, prefer_polling=True)
        stop_watching()
        stop_watching()  # second call must not raise


# ---------------------------------------------------------------------------
# Default config dir
# ---------------------------------------------------------------------------


class TestDefaultConfigDir:
    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path))
        assert default_config_dir() == tmp_path

    def test_default_is_axiom_home_config(self, monkeypatch):
        monkeypatch.delenv("AXIOM_CONFIG_DIR", raising=False)
        d = default_config_dir()
        assert d.parts[-2:] == (".axi", "config")
