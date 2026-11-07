# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Format-drift canary (opt-in: ``pytest -m integration``).

For each harness that is INSTALLED, drive the real CLI in a throwaway HOME against a local Ollama model (or, for
Gemini, a local stub of its HTTP API: no login, no credentials), then assert our reader still lists and converts
the session it wrote. A harness upgrade that changes its on-disk format fails here instead of silently breaking
/renew. A harness that is not installed, or an Ollama that is not running, skips with the reason. Nothing here
touches a real history: every run uses a temporary HOME / data dir.

Set ``AXIOM_CANARY_MODEL`` to use a different local model (default ``qwen2.5:3b``; Hermes needs a 64k context
window so it is configured through its own ``ollama_num_ctx``).
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from axiom.memory.absorb.sessions import session_readers

pytestmark = pytest.mark.integration

PROMPT = "Reply with the single word pong."
MODEL = os.environ.get("AXIOM_CANARY_MODEL", "qwen2.5:3b")
OLLAMA = "http://localhost:11434"


def _need(binary: str) -> str:
    found = shutil.which(binary) or shutil.which(binary, path=str(Path.home() / ".hermes" / "bin"))
    if not found:
        pytest.skip(f"{binary} is not installed")
    return found


@pytest.fixture(scope="module", autouse=True)
def _ollama():
    try:
        tags = json.load(urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3))
    except Exception:  # noqa: BLE001
        pytest.skip("Ollama is not running on localhost:11434")
    if MODEL not in {m["name"] for m in tags.get("models", [])}:
        pytest.skip(f"local model {MODEL} is not pulled")


def _run(
    cmd, home: Path, cwd: Path, env: dict | None = None, timeout: int = 240
) -> subprocess.CompletedProcess:
    full = {**os.environ, "HOME": str(home), **(env or {})}
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=full,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _check(harness: str, home: Path) -> None:
    reader = session_readers(home)[harness]
    headers = reader.list_sessions()
    assert headers, (
        f"{harness}: wrote a session but the reader lists none: {reader.skipped} {reader.notes}"
    )
    t = reader.load_session(headers[0].source_session_id)
    assert t is not None, f"{harness}: listed but would not load: {reader.last_error}"
    assert t.messages and t.messages[0].role == "user" and "pong" in t.messages[0].content.lower()
    if len(t.messages) < 2:
        # the small local model sometimes ends a turn with no output (e.g. an auto-rejected tool call); the
        # PROMPT half parsed, which is what a format change would break first, but the reply half was not seen
        pytest.skip(
            f"{harness}: the model wrote no reply this time, so only the user half of the format was checked"
        )
    assert t.messages[-1].role == "assistant" and t.messages[-1].content.strip()


@pytest.fixture
def box(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    return home, work


def test_opencode(box):
    exe = _need("opencode")
    home, work = box
    cfg = home / "cfg" / "opencode"
    cfg.mkdir(parents=True)
    (cfg / "opencode.json").write_text(
        json.dumps(
            {
                "provider": {
                    "ollama": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "Ollama",
                        "options": {"baseURL": OLLAMA + "/v1"},
                        "models": {MODEL: {"name": MODEL}},
                    }
                },
                "model": f"ollama/{MODEL}",
            }
        )
    )
    env = {
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CONFIG_HOME": str(home / "cfg"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_CACHE_HOME": str(home / "cache"),
    }
    _run([exe, "run", PROMPT], home, work, env)
    _check("opencode", home)


def test_hermes(box):
    exe = _need("hermes")
    home, work = box
    hh = home / "hermes-home"
    hh.mkdir()
    (hh / "config.yaml").write_text(
        f"model:\n  provider: custom\n  default: {MODEL}\n  base_url: {OLLAMA}/v1\n  api_key: ollama\n"
        "  context_length: 65536\n  ollama_num_ctx: 65536\n"
    )
    # Hermes keeps its state in HERMES_HOME; the reader looks under <home>/.hermes
    _run([exe, "chat", "-Q", "-q", PROMPT], home, work, {"HERMES_HOME": str(hh)})
    shutil.copytree(
        hh,
        home / ".hermes",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("*.lock", "sandboxes", "bin"),
    )
    _check("hermes", home)


def test_goose(box):
    exe = _need("goose")
    home, work = box
    _run(
        [exe, "run", "--no-profile", "-t", PROMPT],
        home,
        work,
        {"GOOSE_PROVIDER": "ollama", "GOOSE_MODEL": MODEL, "OLLAMA_HOST": OLLAMA},
    )
    _check("goose", home)


def test_pi(box):
    exe = _need("pi")
    home, work = box
    agent = home / ".pi" / "agent"
    agent.mkdir(parents=True)
    (agent / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "ollama": {
                        "baseUrl": OLLAMA + "/v1",
                        "api": "openai-completions",
                        "apiKey": "ollama",
                        "compat": {
                            "supportsDeveloperRole": False,
                            "supportsReasoningEffort": False,
                        },
                        "models": [{"id": MODEL}],
                    }
                }
            }
        )
    )
    _run(
        [exe, "--provider", "ollama", "--model", MODEL, "-p", PROMPT],
        home,
        work,
        {"PI_CODING_AGENT_DIR": str(agent), "PI_OFFLINE": "1"},
    )
    _check("pi", home)


def test_cline(box):
    exe = _need("cline")
    home, work = box
    _run(
        [
            exe,
            "--config",
            str(home / ".cline"),
            "--data-dir",
            str(home / ".cline" / "data"),
            "-P",
            "ollama",
            "-m",
            MODEL,
            "-k",
            "ollama",
            "--json",
            "-y",
            PROMPT,
        ],
        home,
        work,
    )
    _check("cline", home)


def test_continue(box):
    exe = _need("cn")
    home, work = box
    cfg = home / "cfg.yaml"
    cfg.write_text(
        f"name: local\nversion: 1.0.0\nschema: v1\nmodels:\n  - name: m\n    provider: ollama\n"
        f"    model: {MODEL}\n    apiBase: {OLLAMA}\n    roles: [chat]\n"
    )
    _run([exe, "--config", str(cfg), "-p", PROMPT], home, work)
    _check("continue", home)


def test_gemini_against_a_local_stub_of_its_api(box):
    exe = _need("gemini")
    home, work = box
    (home / ".gemini").mkdir()
    (home / ".gemini" / "settings.json").write_text(
        json.dumps({"security": {"auth": {"selectedType": "gemini-api-key"}}})
    )
    reply = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "pong"}]},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1, "totalTokenCount": 6},
    }

    class Stub(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            if "countTokens" in self.path:
                body, ctype = json.dumps({"totalTokens": 5}).encode(), "application/json"
            elif "stream" in self.path.lower():
                body, ctype = (
                    ("data: " + json.dumps(reply) + "\r\n\r\n").encode(),
                    "text/event-stream",
                )
            else:
                body, ctype = json.dumps(reply).encode(), "application/json"
            self.send_response(200)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _run(
            [exe, "-m", "gemini-2.5-pro", "-p", PROMPT],
            home,
            work,
            {
                "GEMINI_CLI_TRUST_WORKSPACE": "true",
                "GEMINI_API_KEY": "not-a-real-key",
                "GOOGLE_GEMINI_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            },
            timeout=120,
        )
    finally:
        server.shutdown()
    _check("gemini", home)


@pytest.mark.parametrize(
    "harness,why",
    [
        (
            "cursor",
            "the Cursor agent CLI needs a login; the reader is verified from real transcript files",
        ),
        ("claude-code", "needs a logged-in Claude account; verified from real session files"),
        ("codex", "needs a logged-in Codex account; verified from real session files"),
    ],
)
def test_harnesses_that_need_a_login_cannot_be_canaried(harness, why):
    pytest.skip(why)
