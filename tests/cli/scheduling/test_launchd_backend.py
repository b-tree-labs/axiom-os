# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.cli.scheduling.launchd.LaunchdBackend` (issue #205, slice 8).

LaunchdBackend is the macOS-side `SchedulerBackend` impl. Apple deprecated
cron on macOS in favor of `launchd`; user-scope jobs are described by
`.plist` files dropped under `~/Library/LaunchAgents/` and managed via
`launchctl bootstrap` / `launchctl bootout`.

The artifact format is the rendered plist itself (no separate parse
step like CronBackend needs — the artifact is what gets installed).

Cron expressions translate to launchd's `StartCalendarInterval` (exact
values only) or `StartInterval` (step values on minute). Ranges/lists
in cron expressions error clearly — full cron isn't expressible in
launchd's scheduling grammar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

LABEL_PREFIX = "com.axiom.schedule."


# ---------------------------------------------------------------------------
# Test double — MockRunner with scripted replies
# ---------------------------------------------------------------------------


@dataclass
class CapturedRun:
    command: Any
    input: str | None


@dataclass
class CapturedWrite:
    remote_path: str
    content: str


@dataclass
class MockRunner:
    host: str = "localhost"
    runs: list[CapturedRun] = field(default_factory=list)
    writes: list[CapturedWrite] = field(default_factory=list)
    replies: list[tuple[str, str, int]] = field(default_factory=list)

    def run(self, command, *, input=None):
        from axiom.cli.scheduling.protocols import CompletedRun
        self.runs.append(CapturedRun(command=command, input=input))
        if self.replies:
            stdout, stderr, rc = self.replies.pop(0)
            return CompletedRun(stdout=stdout, stderr=stderr, returncode=rc)
        return CompletedRun(stdout="", stderr="", returncode=0)

    def write_file(self, remote_path, content):
        self.writes.append(CapturedWrite(remote_path=remote_path, content=content))


def _probe_replies(*, home="/Users/test", uid="501", agents_dir=None,
                   existing_plist=None):
    """Default reply queue for `_resolve_targets` + idempotence probes.

    LaunchdBackend on install_artifact:
      1. resolve agents dir (env override or $HOME/Library/LaunchAgents)
      2. resolve uid (id -u)
      3. cat existing plist (idempotence probe)
      4. launchctl list (loaded probe) — only when cat matched

    Pass `existing_plist` to simulate a matching prior install (so the
    idempotence skip path triggers). Default: empty/missing, so the
    full install cycle runs.
    """
    target_dir = agents_dir if agents_dir is not None else f"{home}/Library/LaunchAgents"
    replies = [
        (target_dir + "\n", "", 0),  # resolve agents dir
        (uid + "\n", "", 0),          # id -u
    ]
    if existing_plist is None:
        replies.append(("", "", 1))  # cat: file missing → not idempotent
    else:
        replies.append((existing_plist, "", 0))  # cat: file present
        replies.append(("", "", 0))   # launchctl list: loaded
    return replies


# ---------------------------------------------------------------------------
# Static shape
# ---------------------------------------------------------------------------


class TestStaticShape:
    def test_conforms_to_scheduler_backend(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        from axiom.cli.scheduling.protocols import SchedulerBackend
        assert isinstance(LaunchdBackend(), SchedulerBackend)

    def test_name_is_launchd(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        assert LaunchdBackend().name == "launchd"

    def test_artifact_filename_is_dot_plist(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        assert LaunchdBackend().artifact_filename("heartbeat") == "heartbeat.plist"


# ---------------------------------------------------------------------------
# Render — cron → launchd translation + plist shape
# ---------------------------------------------------------------------------


class TestRender:
    def test_label_uses_axiom_schedule_prefix(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        out = LaunchdBackend().render(
            schedule_name="heartbeat",
            cron="0 12 * * *",
            command="echo hi",
        )
        assert "<key>Label</key>" in out
        assert f"{LABEL_PREFIX}heartbeat" in out

    def test_program_arguments_wraps_command_in_sh_c(self):
        """Cron-style commands often have shell features (pipes, env vars).
        ProgramArguments must invoke `/bin/sh -c <command>` so they parse
        the same way they would under cron."""
        from axiom.cli.scheduling.launchd import LaunchdBackend
        out = LaunchdBackend().render(
            schedule_name="x",
            cron="0 12 * * *",
            command="echo $HOME | head",
        )
        assert "<key>ProgramArguments</key>" in out
        assert "/bin/sh" in out
        assert "<string>-c</string>" in out
        # The command is preserved as a single string argument
        assert "echo $HOME | head" in out

    def test_exact_value_cron_emits_start_calendar_interval(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        out = LaunchdBackend().render(
            schedule_name="x",
            cron="0 12 * * *",
            command="echo hi",
        )
        assert "<key>StartCalendarInterval</key>" in out
        assert "<key>Minute</key>" in out
        assert "<integer>0</integer>" in out
        assert "<key>Hour</key>" in out
        assert "<integer>12</integer>" in out
        # Day/Month/Weekday are `*` → omitted
        assert "<key>Day</key>" not in out
        assert "<key>Weekday</key>" not in out

    def test_step_minute_cron_emits_start_interval(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        out = LaunchdBackend().render(
            schedule_name="x",
            cron="*/15 * * * *",
            command="echo hi",
        )
        # */15 → every 15 minutes → StartInterval=900 seconds
        assert "<key>StartInterval</key>" in out
        assert "<integer>900</integer>" in out

    def test_macro_hourly(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        out = LaunchdBackend().render(
            schedule_name="x", cron="@hourly", command="echo",
        )
        assert "<key>StartCalendarInterval</key>" in out
        assert "<key>Minute</key>" in out
        assert "<integer>0</integer>" in out

    def test_macro_daily(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        out = LaunchdBackend().render(
            schedule_name="x", cron="@daily", command="echo",
        )
        # @daily ≡ 0 0 * * * → Minute=0, Hour=0
        assert out.count("<integer>0</integer>") >= 2

    def test_range_cron_rejected(self):
        """Ranges (`0-30 * * * *`) aren't expressible in launchd. Error
        clearly so the user sees the gap instead of getting a silently
        broken schedule."""
        from axiom.cli.scheduling.launchd import LaunchdBackend
        with pytest.raises(ValueError, match="range"):
            LaunchdBackend().render(
                schedule_name="x", cron="0-30 * * * *", command="echo",
            )

    def test_list_cron_rejected(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        with pytest.raises(ValueError, match="list"):
            LaunchdBackend().render(
                schedule_name="x", cron="0,15,30 * * * *", command="echo",
            )

    def test_step_on_non_minute_rejected(self):
        """Step values on hour/day/etc. aren't supported in this first
        cut. Minute steps map cleanly to StartInterval."""
        from axiom.cli.scheduling.launchd import LaunchdBackend
        with pytest.raises(ValueError, match="step"):
            LaunchdBackend().render(
                schedule_name="x", cron="0 */6 * * *", command="echo",
            )

    def test_render_is_well_formed_xml(self):
        """Quick well-formedness sanity check via xml.etree."""
        import xml.etree.ElementTree as ET
        from axiom.cli.scheduling.launchd import LaunchdBackend
        out = LaunchdBackend().render(
            schedule_name="x", cron="0 12 * * *", command="echo hi",
        )
        # plist DOCTYPE makes etree fussy; strip preamble
        root = ET.fromstring(out[out.index("<plist"):])
        assert root.tag == "plist"


# ---------------------------------------------------------------------------
# install_artifact — full host-side lifecycle
# ---------------------------------------------------------------------------


class TestInstallArtifact:
    def test_resolves_target_path_then_bootout_then_write_then_bootstrap(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        runner = MockRunner(host="localhost")
        runner.replies = _probe_replies(home="/Users/test", uid="501")
        # Allow the bootout call to "fail with not-loaded" — common when
        # this is a fresh install.
        runner.replies.append(("", "Could not find specified service\n", 113))
        # bootstrap succeeds
        runner.replies.append(("", "", 0))

        artifact = "<?xml version='1.0'?>\n<plist>...</plist>"
        LaunchdBackend().install_artifact(
            runner=runner,
            schedule_name="heartbeat",
            artifact_content=artifact,
        )

        # The plist was written under the resolved LaunchAgents dir
        assert len(runner.writes) == 1
        assert runner.writes[0].remote_path == (
            "/Users/test/Library/LaunchAgents/com.axiom.schedule.heartbeat.plist"
        )
        assert runner.writes[0].content == artifact

        # The runner sequence: probe agents dir → id -u → bootout → bootstrap
        cmds = [r.command for r in runner.runs]
        assert any("bootout" in str(c) for c in cmds)
        assert any("bootstrap" in str(c) for c in cmds)
        # Bootstrap targets `gui/501` and the resolved plist path
        bootstrap_call = next(c for c in cmds if "bootstrap" in str(c))
        joined = " ".join(bootstrap_call) if isinstance(bootstrap_call, list) else bootstrap_call
        assert "gui/501" in joined
        assert "/Users/test/Library/LaunchAgents/com.axiom.schedule.heartbeat.plist" in joined

    def test_honors_AXIOM_LAUNCH_AGENTS_DIR_override(self):
        """Test-only override so smokes can target a tmp dir instead of
        the user's real `~/Library/LaunchAgents/`."""
        from axiom.cli.scheduling.launchd import LaunchdBackend
        runner = MockRunner(host="localhost")
        # The first probe call resolves the override.
        runner.replies = _probe_replies(agents_dir="/tmp/test-agents", uid="501")
        runner.replies.append(("", "", 113))  # bootout
        runner.replies.append(("", "", 0))     # bootstrap

        LaunchdBackend().install_artifact(
            runner=runner,
            schedule_name="x",
            artifact_content="<plist/>",
        )
        assert runner.writes[0].remote_path.startswith("/tmp/test-agents/")

    def test_bootout_failure_for_not_loaded_is_tolerated(self):
        """Fresh install — bootout will fail because no prior job. Must
        not raise; bootstrap should still run."""
        from axiom.cli.scheduling.launchd import LaunchdBackend
        runner = MockRunner(host="localhost")
        runner.replies = _probe_replies()
        runner.replies.append(("", "Could not find service\n", 113))  # bootout failure
        runner.replies.append(("", "", 0))  # bootstrap

        LaunchdBackend().install_artifact(
            runner=runner, schedule_name="x", artifact_content="<plist/>",
        )
        # No exception
        cmds = [str(r.command) for r in runner.runs]
        assert any("bootstrap" in c for c in cmds)

    def test_bootstrap_failure_raises(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        runner = MockRunner(host="localhost")
        runner.replies = _probe_replies()
        runner.replies.append(("", "", 113))   # bootout (tolerated)
        runner.replies.append(("", "Load failed: 5: Input/output error\n", 5))

        with pytest.raises(RuntimeError, match="bootstrap"):
            LaunchdBackend().install_artifact(
                runner=runner, schedule_name="x", artifact_content="<plist/>",
            )


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


class TestIdempotence:
    """macOS surfaces the Login Items toast on every plist write +
    `launchctl load/bootstrap`. install_artifact must skip the whole
    bootout+write+bootstrap cycle when the existing plist content
    already matches AND the agent is already loaded — otherwise
    `axi schedule install` becomes a recurring noise source (issue #208)."""

    def test_skips_install_when_plist_matches_and_loaded(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        artifact = "<?xml version='1.0'?>\n<plist>...</plist>\n"

        runner = MockRunner(host="localhost")
        # existing_plist matches → cat probe matches → list probe runs → loaded → skip
        runner.replies = _probe_replies(existing_plist=artifact)
        # NO further calls expected — no bootout, no write, no bootstrap

        LaunchdBackend().install_artifact(
            runner=runner, schedule_name="x", artifact_content=artifact,
        )

        assert runner.writes == [], "plist rewritten despite matching content"
        cmds = [str(r.command) for r in runner.runs]
        assert not any("bootstrap" in c for c in cmds), (
            f"bootstrap fired despite matching plist + loaded state: {cmds}"
        )

    def test_does_full_cycle_when_plist_differs(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        runner = MockRunner(host="localhost")
        # existing_plist is OLD content → cat probe returns it → mismatch → full cycle
        runner.replies = _probe_replies(existing_plist="<plist>old</plist>")
        # bootout
        runner.replies.append(("", "", 0))
        # bootstrap
        runner.replies.append(("", "", 0))

        LaunchdBackend().install_artifact(
            runner=runner, schedule_name="x", artifact_content="<plist>new</plist>",
        )

        assert len(runner.writes) == 1
        cmds = [str(r.command) for r in runner.runs]
        assert any("bootstrap" in c for c in cmds)


class TestUninstall:
    def test_bootout_then_remove_plist(self):
        from axiom.cli.scheduling.launchd import LaunchdBackend
        runner = MockRunner(host="localhost")
        runner.replies = _probe_replies()
        runner.replies.append(("", "", 0))  # bootout success
        runner.replies.append(("", "", 0))  # rm

        LaunchdBackend().uninstall(runner=runner, schedule_name="heartbeat")

        cmds = [str(r.command) for r in runner.runs]
        # Sequence: probe agents dir → id -u → bootout → rm -f
        assert any("bootout" in c for c in cmds)
        assert any("rm" in c for c in cmds)
        assert any("com.axiom.schedule.heartbeat.plist" in c for c in cmds)

    def test_uninstall_when_not_installed_is_noop(self):
        """No-op when the schedule isn't installed. bootout fails with
        not-found; uninstall must swallow that and proceed to remove
        the (nonexistent) plist file via `rm -f` (which itself swallows
        missing files)."""
        from axiom.cli.scheduling.launchd import LaunchdBackend
        runner = MockRunner(host="localhost")
        runner.replies = _probe_replies()
        runner.replies.append(("", "Could not find service\n", 113))  # bootout
        runner.replies.append(("", "", 0))  # rm -f exits 0 even for missing

        # No exception
        LaunchdBackend().uninstall(runner=runner, schedule_name="not-there")
