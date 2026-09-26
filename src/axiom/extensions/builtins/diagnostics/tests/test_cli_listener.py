# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for TRIAGE's CLI failure listener.

The listener subscribes to `cli.arg_error` events on the bus, runs the
event through `cli_diagnoses.match_failure`, and on hit appends a
record to `~/.axi/agents/triage/pending-diagnoses.jsonl` so the
pre-command hook can surface it on next CLI invocation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.extensions.builtins.diagnostics import cli_listener
from axiom.infra.bus import EventBus


def _bonsai_event_payload() -> dict:
    return {
        "command": "chat",
        "argv": ["axi", "chat"],
        "error_type": "OSError",
        "error_message": "[Errno 22] Invalid argument: bonsai-1.7b.gguf",
        "traceback": "OSError: [Errno 22] bonsai-1.7b.gguf\n",
        "fingerprint": "chat:OSError:bonsai",
        "recovered": False,
        "environment": {"neut_version": "0.13.0"},
        "timestamp": datetime.now(UTC).isoformat(),
    }


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ~/.axi → tmp/.axi so listener's writes stay isolated.

    Returns the resolved state dir (tmp/.axi) so callers using `read_pending`
    or `pending_path` with an explicit `state_dir` argument hit the same
    path the listener wrote to.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Disable the LLM fallback in unit tests — listener tests target the
    # catalog-match path; LLM fallback has its own coverage in
    # test_cli_diagnoses_llm.py.
    monkeypatch.setenv("AXI_DIAGNOSES_NO_LLM", "1")
    return tmp_path / ".axi"


@pytest.fixture
def bus(tmp_path: Path) -> EventBus:
    return EventBus(log_path=tmp_path / "events.jsonl")


class TestListenerWritesPendingDiagnosis:
    def test_bonsai_event_writes_pending_diagnosis(
        self, bus: EventBus, state_dir: Path
    ) -> None:
        cli_listener.register(bus)

        bus.publish("cli.arg_error", _bonsai_event_payload(), source="test")

        pending = cli_listener.read_pending(state_dir)
        assert len(pending) == 1
        assert pending[0]["pattern_id"] == "bonsai-deprecated"
        assert "qwen" in pending[0]["remedy"].lower()

    def test_unmatched_event_writes_nothing(
        self, bus: EventBus, state_dir: Path
    ) -> None:
        cli_listener.register(bus)

        bus.publish(
            "cli.arg_error",
            {
                "command": "ext",
                "error_type": "FileNotFoundError",
                "error_message": "manifest.toml missing",
                "traceback": "",
                "fingerprint": "ext:FileNotFoundError",
                "recovered": False,
                "environment": {},
                "timestamp": datetime.now(UTC).isoformat(),
            },
            source="test",
        )

        assert cli_listener.read_pending(state_dir) == []

    def test_duplicate_events_dedupe_by_fingerprint(
        self, bus: EventBus, state_dir: Path
    ) -> None:
        """Two identical bonsai failures shouldn't pile up two diagnoses."""
        cli_listener.register(bus)

        bus.publish("cli.arg_error", _bonsai_event_payload(), source="test")
        bus.publish("cli.arg_error", _bonsai_event_payload(), source="test")

        pending = cli_listener.read_pending(state_dir)
        assert len(pending) == 1


class TestPendingLifecycle:
    def test_clear_by_fingerprint_removes_one_entry(self, state_dir: Path) -> None:
        cli_listener.append_diagnosis(
            state_dir,
            {"pattern_id": "bonsai-deprecated", "fingerprint": "abc123",
             "summary": "x", "remedy": "y", "confidence": 0.9,
             "matched_at": "2026-05-03T20:00:00Z"},
        )
        cli_listener.append_diagnosis(
            state_dir,
            {"pattern_id": "other", "fingerprint": "xyz789",
             "summary": "x", "remedy": "y", "confidence": 0.9,
             "matched_at": "2026-05-03T20:00:00Z"},
        )

        cli_listener.clear_pending(state_dir, fingerprint="abc123")

        remaining = cli_listener.read_pending(state_dir)
        assert len(remaining) == 1
        assert remaining[0]["fingerprint"] == "xyz789"

    def test_clear_all_empties_log(self, state_dir: Path) -> None:
        cli_listener.append_diagnosis(
            state_dir,
            {"pattern_id": "bonsai-deprecated", "fingerprint": "abc123",
             "summary": "x", "remedy": "y", "confidence": 0.9,
             "matched_at": "2026-05-03T20:00:00Z"},
        )

        cli_listener.clear_pending(state_dir, fingerprint=None)

        assert cli_listener.read_pending(state_dir) == []

    def test_read_pending_handles_missing_file(self, state_dir: Path) -> None:
        # Fresh ~/.axi → no log yet → empty list, not error.
        assert cli_listener.read_pending(state_dir) == []

    def test_read_pending_skips_corrupt_lines(self, state_dir: Path) -> None:
        path = cli_listener.pending_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A real diagnosis followed by a corrupt line and another real one.
        path.write_text(
            json.dumps({"pattern_id": "bonsai-deprecated", "fingerprint": "a",
                        "summary": "s", "remedy": "r", "confidence": 0.9,
                        "matched_at": "2026-05-03T20:00:00Z"}) + "\n"
            "{not json\n" +
            json.dumps({"pattern_id": "other", "fingerprint": "b",
                        "summary": "s", "remedy": "r", "confidence": 0.9,
                        "matched_at": "2026-05-03T20:00:00Z"}) + "\n"
        )

        pending = cli_listener.read_pending(state_dir)
        # Both valid entries returned; corrupt line silently skipped.
        assert {p["fingerprint"] for p in pending} == {"a", "b"}


@pytest.fixture(autouse=True)
def _restore_branding():
    """Registering branding appends to a module-level list that nothing pops.

    These tests deliberately switch brand, and without this every test that
    ran AFTER them saw `neut` — which is how they broke two unrelated
    diagnostics tests that assert on `.axi` paths. Passing alone and failing
    in a suite is the signature.
    """
    from axiom.infra import branding

    saved = list(branding._registered)
    try:
        yield
    finally:
        branding._registered[:] = saved


class TestDiagnosesAreNotSplitByWhichAliasYouTyped:
    """`axi` warned about three pending diagnoses and `neut` reported none.

    Same install, same user, same failures — but the pending file hung off the
    branded state dir, so each alias read a different file. A diagnosis
    describes a failure of this machine, not of a product, so it belongs in
    the platform's own directory.
    """

    def test_the_path_does_not_move_when_the_brand_changes(self, monkeypatch, tmp_path):
        """Deliberately NOT via AXI_STATE_DIR.

        An override points the branded and platform directories at the same
        place, so a test using it passes whichever one the code reads — it
        cannot see the bug at all. Pointing HOME at a temp dir keeps the two
        genuinely different, which is the only way this assertion means
        anything.
        """
        from pathlib import Path

        from axiom.extensions.builtins.diagnostics import cli_listener
        from axiom.infra import branding

        monkeypatch.delenv("AXI_STATE_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        under_axi = cli_listener.pending_path()
        assert ".axi" in str(under_axi), under_axi

        branding.register(
            branding.BrandingConfig(
                cli_name="acme", product_name="Acme OS", package_name="acme-os"
            )
        )
        assert cli_listener.pending_path() == under_axi, (
            f"the diagnoses moved with the brand: {cli_listener.pending_path()}"
        )
        assert ".acme" not in str(cli_listener.pending_path())

    def test_the_platform_directory_is_the_same_under_every_brand(
        self, monkeypatch, tmp_path
    ):
        from pathlib import Path

        from axiom.infra import branding
        from axiom.infra.paths import get_platform_state_dir, get_user_state_dir

        monkeypatch.delenv("AXI_STATE_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        branding.register(
            branding.BrandingConfig(
                cli_name="acme", product_name="Acme OS", package_name="acme-os"
            )
        )
        assert get_platform_state_dir() == tmp_path / ".axi"
        assert get_user_state_dir() == tmp_path / ".acme", (
            "the branded dir should still follow the brand — only the platform "
            "one is fixed"
        )

    def test_records_left_under_a_brand_home_are_adopted(self, monkeypatch, tmp_path):
        """They are real failures somebody should see, so they are merged
        rather than stranded where only one alias can reach them."""
        import json

        from axiom.extensions.builtins.diagnostics import cli_listener

        platform = tmp_path / "platform"
        branded = tmp_path / "branded"
        monkeypatch.setattr(cli_listener, "pending_path", lambda sd=None: (
            (sd or platform) / "agents" / "triage" / cli_listener.PENDING_FILENAME
        ))
        legacy = branded / "agents" / "triage" / cli_listener.PENDING_FILENAME
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"fingerprint": "aaa", "summary": "old"}) + "\n")

        monkeypatch.setattr(
            "axiom.infra.paths.get_platform_state_dir", lambda: platform
        )
        monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: branded)

        cli_listener._adopt_branded_diagnoses()

        assert [d["fingerprint"] for d in cli_listener.read_pending()] == ["aaa"]
        assert not legacy.exists(), (
            "the brand-local file must go, or the split returns on the next read"
        )

    def test_adoption_does_not_duplicate_what_is_already_shared(
        self, monkeypatch, tmp_path
    ):
        import json

        from axiom.extensions.builtins.diagnostics import cli_listener

        platform = tmp_path / "platform"
        branded = tmp_path / "branded"
        monkeypatch.setattr(cli_listener, "pending_path", lambda sd=None: (
            (sd or platform) / "agents" / "triage" / cli_listener.PENDING_FILENAME
        ))
        shared = platform / "agents" / "triage" / cli_listener.PENDING_FILENAME
        shared.parent.mkdir(parents=True)
        shared.write_text(json.dumps({"fingerprint": "aaa", "summary": "known"}) + "\n")
        legacy = branded / "agents" / "triage" / cli_listener.PENDING_FILENAME
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"fingerprint": "aaa", "summary": "same"}) + "\n")

        monkeypatch.setattr("axiom.infra.paths.get_platform_state_dir", lambda: platform)
        monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: branded)

        cli_listener._adopt_branded_diagnoses()
        assert len(cli_listener.read_pending()) == 1

    def test_adoption_is_silent_when_the_dirs_are_the_same(self, monkeypatch, tmp_path):
        """Under the platform's own brand there is nothing to adopt, and it
        must not delete the file it just read."""
        import json

        from axiom.extensions.builtins.diagnostics import cli_listener

        monkeypatch.setattr("axiom.infra.paths.get_platform_state_dir", lambda: tmp_path)
        monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: tmp_path)
        path = tmp_path / "agents" / "triage" / cli_listener.PENDING_FILENAME
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"fingerprint": "keep", "summary": "x"}) + "\n")
        monkeypatch.setattr(cli_listener, "pending_path", lambda sd=None: path)

        cli_listener._adopt_branded_diagnoses()
        assert path.exists() and len(cli_listener.read_pending()) == 1
