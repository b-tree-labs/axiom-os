# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.cli.scheduling.local.LocalRunner` (issue #205, slice 7).

`LocalRunner` is the third `RemoteRunner` Protocol impl (after `SSHRunner`
and the test-only `MockRunner`). It executes commands via plain
`subprocess.run` against the host the CLI is running on — no ssh round-
trip. The motivating case: `axi schedule install --host localhost`
when the schedule target is the user's own macOS laptop (LaunchdBackend
against `~/Library/LaunchAgents/` on this machine). For that flow,
going through ssh-to-self is a needless dependency on sshd configuration
+ key auth.

The Runner is intentionally narrow: same `run()` + `write_file()` shape
as SSHRunner so the orchestrator can swap transports without caring
which one it has. The CLI picks LocalRunner when `--host` ∈
{"localhost", "local"}; everything else gets SSHRunner.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class FakeCompletedProcess:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


@dataclass
class SubprocessCapture:
    args: tuple
    kwargs: dict


@pytest.fixture
def captured_runs(monkeypatch):
    captures: list[SubprocessCapture] = []
    replies: list[FakeCompletedProcess] = []

    def fake_run(*args, **kwargs):
        captures.append(SubprocessCapture(args=args, kwargs=kwargs))
        if replies:
            return replies.pop(0)
        return FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captures, replies


class TestConforms:
    def test_isinstance_remoterunner(self):
        from axiom.cli.scheduling.local import LocalRunner
        from axiom.cli.scheduling.protocols import RemoteRunner
        assert isinstance(LocalRunner(), RemoteRunner)


class TestHostAttr:
    def test_default_host_is_localhost(self):
        from axiom.cli.scheduling.local import LocalRunner
        assert LocalRunner().host == "localhost"

    def test_host_can_be_overridden_for_display(self):
        """LocalRunner is identified by `host` in install reports; allow
        either 'localhost' or 'local' as a display value. Anything else
        is suspicious (looks like a typo for a remote target)."""
        from axiom.cli.scheduling.local import LocalRunner
        assert LocalRunner(host="local").host == "local"

    def test_remote_looking_host_rejected(self):
        """Refuse `LocalRunner(host="test-host")` — it would silently run
        remote-targeted commands on the local box. Force the caller to
        use SSHRunner for any non-local host."""
        from axiom.cli.scheduling.local import LocalRunner
        with pytest.raises(ValueError, match="localhost"):
            LocalRunner(host="test-host")


class TestRunListCommand:
    def test_runs_argv_directly_no_ssh_wrapper(self, captured_runs):
        from axiom.cli.scheduling.local import LocalRunner
        captures, _ = captured_runs

        LocalRunner().run(["echo", "hi"])

        assert len(captures) == 1
        argv = captures[0].args[0]
        # No ssh prefix; the argv runs locally as-is.
        assert argv == ["echo", "hi"]

    def test_captures_stdout_stderr(self, captured_runs):
        from axiom.cli.scheduling.local import LocalRunner
        captures, replies = captured_runs
        replies.append(FakeCompletedProcess(
            stdout="hello\n", stderr="warn\n", returncode=0,
        ))

        result = LocalRunner().run(["echo", "hi"])
        assert result.stdout == "hello\n"
        assert result.stderr == "warn\n"
        assert result.returncode == 0
        assert result.ok is True

    def test_non_zero_returncode_surfaces(self, captured_runs):
        from axiom.cli.scheduling.local import LocalRunner
        captures, replies = captured_runs
        replies.append(FakeCompletedProcess(stdout="", stderr="boom", returncode=2))

        result = LocalRunner().run(["false"])
        assert result.returncode == 2
        assert result.ok is False
        assert result.stderr == "boom"


class TestRunStringCommand:
    def test_string_command_runs_via_shell(self, captured_runs):
        """List → exec directly; string → run via shell. Same shape SSH
        runner offers (string callers expect shell parsing)."""
        from axiom.cli.scheduling.local import LocalRunner
        captures, _ = captured_runs
        LocalRunner().run("crontab -l")

        argv = captures[0].args[0]
        kw = captures[0].kwargs
        # Either shell=True with the raw string, or argv shape via /bin/sh -c
        assert kw.get("shell") is True or argv[:2] == ["/bin/sh", "-c"]


class TestRunWithInput:
    def test_input_piped_via_subprocess_input_kwarg(self, captured_runs):
        from axiom.cli.scheduling.local import LocalRunner
        captures, _ = captured_runs
        LocalRunner().run(["crontab", "-"], input="*/15 * * * * echo\n")
        assert captures[0].kwargs.get("input") == "*/15 * * * * echo\n"


class TestSubprocessKwargs:
    def test_capture_text_mode(self, captured_runs):
        from axiom.cli.scheduling.local import LocalRunner
        captures, _ = captured_runs
        LocalRunner().run(["echo"])

        kw = captures[0].kwargs
        assert kw.get("capture_output") is True
        assert kw.get("text") is True

    def test_check_is_false(self, captured_runs):
        from axiom.cli.scheduling.local import LocalRunner
        captures, _ = captured_runs
        LocalRunner().run(["false"])
        assert captures[0].kwargs.get("check") in (False, None)


class TestWriteFile:
    def test_writes_text_to_local_path(self, tmp_path: Path):
        from axiom.cli.scheduling.local import LocalRunner
        out = tmp_path / "subdir" / "file.txt"
        LocalRunner().write_file(remote_path=str(out), content="hello\n")
        assert out.read_text() == "hello\n"

    def test_write_file_creates_parent_dirs(self, tmp_path: Path):
        from axiom.cli.scheduling.local import LocalRunner
        out = tmp_path / "a" / "b" / "c" / "f.txt"
        LocalRunner().write_file(remote_path=str(out), content="x")
        assert out.read_text() == "x"

    def test_write_file_raises_on_failure(self, tmp_path: Path, monkeypatch):
        """A real OS write error (e.g. permission denied) must surface
        as RuntimeError so the orchestrator can flag the schedule."""
        from axiom.cli.scheduling import local

        def fake_write_text(self, *args, **kwargs):
            raise PermissionError("Permission denied")

        monkeypatch.setattr(Path, "write_text", fake_write_text)
        with pytest.raises(RuntimeError, match="Permission denied"):
            local.LocalRunner().write_file(
                remote_path=str(tmp_path / "f.txt"),
                content="x",
            )
