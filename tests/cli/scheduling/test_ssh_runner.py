# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.cli.scheduling.ssh.SSHRunner` (issue #205, slice 2).

`SSHRunner` is one concrete impl of the `RemoteRunner` Protocol from
slice 1. Talks to a remote host via the `ssh` CLI. Path operations
come through `run()` with host-native commands; binary file transfer
isn't in scope (use `write_file()` for the small text artifacts the
scheduler needs).

Tests stub `subprocess.run` so they don't touch the network. A
separate integration smoke (gated by `RUN_SSH_TESTS=1`) covers the
real-network path in slice 6 against the configured AXIOM_TEST_HOST.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class FakeCompletedProcess:
    """Stand-in for `subprocess.CompletedProcess`."""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


@dataclass
class SubprocessCapture:
    args: tuple
    kwargs: dict


@pytest.fixture
def captured_runs(monkeypatch):
    """Replace `subprocess.run` with a recorder + scripted reply queue."""
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
        from axiom.cli.scheduling.protocols import RemoteRunner
        from axiom.cli.scheduling.ssh import SSHRunner
        assert isinstance(SSHRunner(host="test-host"), RemoteRunner)


class TestRunListCommand:
    def test_invokes_ssh_with_host_and_command_args(self, captured_runs):
        from axiom.cli.scheduling.ssh import SSHRunner
        captures, _ = captured_runs

        SSHRunner(host="test-host").run(["echo", "hi"])

        assert len(captures) == 1
        argv = captures[0].args[0]
        # `ssh <host> echo hi` (or with -o options interleaved)
        assert argv[0] == "ssh"
        assert "test-host" in argv
        # The user's command arguments are at the tail
        assert argv[-2:] == ["echo", "hi"]

    def test_captures_stdout_stderr(self, captured_runs):
        from axiom.cli.scheduling.ssh import SSHRunner
        captures, replies = captured_runs
        replies.append(FakeCompletedProcess(
            stdout="hello\n", stderr="warn\n", returncode=0,
        ))

        result = SSHRunner(host="test-host").run(["echo", "hi"])
        assert result.stdout == "hello\n"
        assert result.stderr == "warn\n"
        assert result.returncode == 0
        assert result.ok is True

    def test_non_zero_returncode_surfaces(self, captured_runs):
        from axiom.cli.scheduling.ssh import SSHRunner
        captures, replies = captured_runs
        replies.append(FakeCompletedProcess(stdout="", stderr="boom", returncode=2))

        result = SSHRunner(host="test-host").run(["false"])
        assert result.returncode == 2
        assert result.ok is False
        assert result.stderr == "boom"


class TestRunStringCommand:
    """Some callers pass a single shell-string instead of an argv list
    (e.g. `crontab -l && crontab -l | head`). Accept both shapes."""

    def test_string_command_passed_through_to_remote_shell(self, captured_runs):
        from axiom.cli.scheduling.ssh import SSHRunner
        captures, _ = captured_runs
        SSHRunner(host="test-host").run("crontab -l")

        argv = captures[0].args[0]
        # When the caller hands a string, we wrap it as the single
        # tail-arg to ssh — the remote shell parses it.
        assert argv[0] == "ssh"
        assert argv[-1] == "crontab -l"


class TestRunWithInput:
    def test_input_is_piped_via_subprocess_input_kwarg(self, captured_runs):
        from axiom.cli.scheduling.ssh import SSHRunner
        captures, _ = captured_runs
        payload = "*/15 * * * * echo\n"
        SSHRunner(host="test-host").run(["crontab", "-"], input=payload)

        assert captures[0].kwargs.get("input") == payload


class TestSshOptions:
    def test_batch_mode_for_no_password_prompts(self, captured_runs):
        """Tests are non-interactive; batch mode prevents password
        prompts that would hang. Real-host smoke uses the same flag —
        it's a strict-mode default the user opts out of explicitly."""
        from axiom.cli.scheduling.ssh import SSHRunner
        captures, _ = captured_runs
        SSHRunner(host="test-host").run(["echo", "hi"])

        argv = captures[0].args[0]
        assert "-o" in argv
        # BatchMode=yes appears as a `-o` value somewhere in the call
        idx = argv.index("-o")
        assert "BatchMode=yes" in argv[idx + 1] or any(
            "BatchMode=yes" in arg for arg in argv
        )

    def test_capture_text_mode(self, captured_runs):
        from axiom.cli.scheduling.ssh import SSHRunner
        captures, _ = captured_runs
        SSHRunner(host="test-host").run(["echo", "hi"])

        kw = captures[0].kwargs
        assert kw.get("capture_output") is True
        assert kw.get("text") is True

    def test_check_is_false(self, captured_runs):
        """Caller decides whether non-zero is an error. SSHRunner must
        never raise on rc != 0 — surface the rc instead."""
        from axiom.cli.scheduling.ssh import SSHRunner
        captures, _ = captured_runs
        SSHRunner(host="test-host").run(["false"])

        assert captures[0].kwargs.get("check") in (False, None)


class TestWriteFile:
    def test_writes_via_ssh_cat_redirect(self, captured_runs):
        from axiom.cli.scheduling.ssh import SSHRunner
        captures, _ = captured_runs

        SSHRunner(host="test-host").write_file(
            remote_path="/tmp/test.txt",
            content="hello world\n",
        )

        assert len(captures) == 1
        argv = captures[0].args[0]
        assert argv[0] == "ssh"
        assert "test-host" in argv
        # Tail arg is the shell command that redirects stdin to the file.
        # Quoting the path defends against spaces/special chars.
        tail = argv[-1]
        assert "cat" in tail
        assert ">" in tail
        assert shlex.quote("/tmp/test.txt") in tail
        # Content passed via stdin
        assert captures[0].kwargs.get("input") == "hello world\n"

    def test_paths_with_spaces_are_quoted(self, captured_runs):
        from axiom.cli.scheduling.ssh import SSHRunner
        captures, _ = captured_runs

        SSHRunner(host="test-host").write_file(
            remote_path="/tmp/has space.txt",
            content="x",
        )

        tail = captures[0].args[0][-1]
        assert "'/tmp/has space.txt'" in tail or '"/tmp/has space.txt"' in tail

    def test_write_file_raises_on_failure(self, captured_runs):
        from axiom.cli.scheduling.ssh import SSHRunner
        captures, replies = captured_runs
        replies.append(FakeCompletedProcess(
            stdout="", stderr="Permission denied", returncode=1,
        ))

        with pytest.raises(RuntimeError, match="Permission denied"):
            SSHRunner(host="test-host").write_file(
                remote_path="/etc/shadow",
                content="x",
            )


class TestHostAttr:
    def test_host_stored_on_runner(self):
        from axiom.cli.scheduling.ssh import SSHRunner
        runner = SSHRunner(host="test-host")
        assert runner.host == "test-host"
