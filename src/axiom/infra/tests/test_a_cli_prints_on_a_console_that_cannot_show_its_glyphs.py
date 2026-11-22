# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A command that prints an arrow or a warning sign to a console that cannot encode it prints ``?`` instead of dying.

Where Python writes through the locale encoding (cp1252 on Windows under Git Bash, a redirected stream, some IDE
terminals), the ingest dry run's preflight line (``ingest → ...``) and the HTTP chat server's start-up warning
(``⚠️ ...``) raised UnicodeEncodeError before the command did anything. Chat already guarded its streams this way
(``tolerate_unencodable_output``); the helper now lives with the other CLI output helpers and both entry points call it.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys


def _cp1252_stream():
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\n", write_through=True)


def test_the_helper_replaces_what_the_stream_cannot_encode():
    from axiom.infra.cli_format import tolerate_unencodable_output

    raw, stream = _cp1252_stream()
    tolerate_unencodable_output([stream])
    stream.write("ingest → rag-org\n")
    assert raw.getvalue().decode("cp1252") == "ingest ? rag-org\n"


def test_the_ingest_dry_run_prints_its_preflight_on_a_cp1252_console(tmp_path):
    (tmp_path / "a.md").write_text("# Doc\n\nsome content for the preflight scan")
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    env.pop("DATABASE_URL", None)
    r = subprocess.run(
        [sys.executable, "-m", "axiom.rag", "ingest", str(tmp_path), "--dry-run", "--corpus", "rag-org"],
        capture_output=True,
        timeout=120,
        env=env,
    )
    out = (r.stdout + r.stderr).decode("cp1252", errors="replace")
    assert "UnicodeEncodeError" not in out
    assert "Preflight" in out


def test_the_http_server_prints_its_start_up_warning_on_a_cp1252_console(monkeypatch):
    from axiom.extensions.builtins.http import chat_server, serve_cli

    raw, stream = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    class _Server:
        def __init__(self, **_kw):
            pass

        def serve(self):
            print("  ⚠️  listening on every interface")

    monkeypatch.setattr(chat_server, "NeutAPIServer", _Server)
    monkeypatch.setattr(sys, "argv", ["serve_cli"])
    serve_cli.main()
    assert raw.getvalue().decode("cp1252").startswith("  ?")
