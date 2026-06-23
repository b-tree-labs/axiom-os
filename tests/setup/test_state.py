# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.setup.state."""

from datetime import UTC, datetime, timedelta

from axiom.setup.state import (
    SetupState,
    clear_state,
    load_state,
    save_state,
)


class TestSetupState:
    def test_default_state(self):
        state = SetupState()
        assert state.current_phase == "probe"
        assert state.completed_phases == []
        assert state.created_at != ""

    def test_mark_phase_complete(self):
        state = SetupState()
        state.mark_phase_complete("probe")
        assert "probe" in state.completed_phases
        assert state.updated_at != ""

    def test_mark_phase_idempotent(self):
        state = SetupState()
        state.mark_phase_complete("probe")
        state.mark_phase_complete("probe")
        assert state.completed_phases.count("probe") == 1

    def test_is_phase_complete(self):
        state = SetupState()
        assert state.is_phase_complete("probe") is False
        state.mark_phase_complete("probe")
        assert state.is_phase_complete("probe") is True

    def test_is_stale_fresh(self):
        state = SetupState()
        assert state.is_stale() is False

    def test_is_stale_old(self):
        old_time = (
            datetime.now(UTC) - timedelta(days=31)
        ).isoformat()
        state = SetupState(created_at=old_time)
        assert state.is_stale() is True

    def test_is_stale_bad_timestamp(self):
        state = SetupState(created_at="not-a-date")
        assert state.is_stale() is True

    def test_roundtrip(self):
        state = SetupState(
            current_phase="credentials",
            completed_phases=["probe", "summary"],
            credentials_configured={"GITLAB_TOKEN": True},
            config_files_created={"facility.toml": True},
            test_results={"gitlab": "pass"},
            user_choices={"facility_type": "research"},
        )
        d = state.to_dict()
        restored = SetupState.from_dict(d)
        assert restored.current_phase == "credentials"
        assert restored.completed_phases == ["probe", "summary"]
        assert restored.credentials_configured["GITLAB_TOKEN"] is True
        assert restored.test_results["gitlab"] == "pass"

    def test_from_dict_defaults(self):
        state = SetupState.from_dict({})
        assert state.current_phase == "probe"
        assert state.completed_phases == []


class TestPersistence:
    def test_save_and_load(self, tmp_path):
        state = SetupState(current_phase="config")
        state.mark_phase_complete("probe")
        save_state(state, tmp_path)

        loaded = load_state(tmp_path)
        assert loaded is not None
        assert loaded.current_phase == "config"
        assert "probe" in loaded.completed_phases

    def test_load_no_file(self, tmp_path):
        assert load_state(tmp_path) is None

    def test_load_corrupt_file(self, tmp_path):
        state_path = tmp_path / ".neut" / "setup-state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text("not json", encoding="utf-8")
        assert load_state(tmp_path) is None

    def test_load_stale_state(self, tmp_path):
        old_time = (
            datetime.now(UTC) - timedelta(days=31)
        ).isoformat()
        state = SetupState(created_at=old_time)
        save_state(state, tmp_path)

        loaded = load_state(tmp_path)
        assert loaded is None
        # State file should be cleaned up
        assert not (tmp_path / ".neut" / "setup-state.json").exists()

    def test_clear_state(self, tmp_path):
        state = SetupState()
        save_state(state, tmp_path)
        assert (tmp_path / ".neut" / "setup-state.json").exists()

        clear_state(tmp_path)
        assert not (tmp_path / ".neut" / "setup-state.json").exists()

    def test_clear_state_no_file(self, tmp_path):
        # Should not raise
        clear_state(tmp_path)
