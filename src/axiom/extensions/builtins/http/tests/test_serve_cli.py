# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Regression test for the http extension's serve_cli entry point.

The bug this guards against (encountered 2026-05-22, axiom-os-lm 0.19.0):

    # serve_cli.py
    def main():
        parser = get_parser()
        args = parser.parse_args()
        from .server import NeutAPIServer   # <-- WRONG MODULE
        server = NeutAPIServer(...)
        server.serve()

`NeutAPIServer` actually lives in `.chat_server`, not `.server`. The bad
import is local to `main()` and runs *after* argparse, so:

  - `axi serve --help` returns from argparse before line 55 and looks
    clean.
  - Plain module imports (`import axiom.extensions.builtins.http.serve_cli`)
    don't trigger the bad import either — it's lexically inside `main()`.
  - Unit tests that exercise `create_app` / `ThreadedServer` from the
    `.server` module pass too, because the bug is in the CLI's *import
    of* `NeutAPIServer`, not in `NeutAPIServer` itself.

So a buggy build can pass every existing http-extension test, every
`--help` smoke, every "module imports cleanly" check, and still
crash-loop on the first non-help invocation. The only way to catch
the bug is to subprocess-invoke `serve_cli` past its argparse path,
let it run long enough to hit the local import, and assert it stays
up.

This test is intentionally subprocess-based per the project convention
that every CLI verb needs an end-to-end test running the entry point
as a separate process.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time

import pytest


def _free_port() -> int:
    """Return a port the OS thinks is unused right now.

    Race-free enough for a single test invocation; the kernel re-uses
    the port immediately after the socket closes.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.timeout(20)
def test_serve_cli_resolves_NeutAPIServer_import():
    """`python -m axiom.extensions.builtins.http.serve_cli` must run past
    its local `from .chat_server import NeutAPIServer` without exiting.

    A surviving subprocess after 2 seconds proves the local import in
    main() resolved. If the import path regresses, the subprocess exits
    within ~100ms with status 1 and an ImportError on stderr; we capture
    and report that as the failure.
    """
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "axiom.extensions.builtins.http.serve_cli",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(2.0)
        early_exit = proc.poll()
        if early_exit is not None:
            stdout, stderr = proc.communicate(timeout=5)
            stderr_text = stderr.decode("utf-8", errors="replace")
            stdout_text = stdout.decode("utf-8", errors="replace")

            # Surface the exact failure mode the historical bug produces,
            # so a future regression is diagnostic-by-default.
            assert "NeutAPIServer" not in stderr_text or "ImportError" not in stderr_text, (
                "serve_cli failed to import NeutAPIServer. This is the "
                "exact regression this test guards against: a local "
                "import in main() points at the wrong module.\n"
                f"stderr:\n{stderr_text}"
            )
            pytest.fail(
                f"serve_cli exited with code {early_exit} before SIGTERM. "
                f"Expected the process to bind and run.\n"
                f"stdout:\n{stdout_text}\n"
                f"stderr:\n{stderr_text}"
            )
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.timeout(10)
def test_serve_cli_help_still_works():
    """Companion sanity check: `--help` must continue to work and exit 0.

    This is the *easy* path the historical bug let pass. Keep it green
    so the harder test above is the one that does the real work."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "axiom.extensions.builtins.http.serve_cli",
            "--help",
        ],
        capture_output=True,
        timeout=8,
        text=True,
    )
    assert result.returncode == 0, f"--help should exit 0, got {result.returncode}\nstderr:\n{result.stderr}"
    assert "--port" in result.stdout
    assert "--host" in result.stdout
