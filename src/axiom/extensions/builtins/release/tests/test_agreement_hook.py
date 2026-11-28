# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The hook is what writes the evidence, so it has to be small, silent and append-only (#2471).

``UserPromptSubmit`` gives a hook ``session_id``, ``prompt_id``, ``transcript_path``,
``cwd``, ``permission_mode`` and ``prompt`` on stdin (code.claude.com/docs/en/hooks).
It also fires for a scheduled task, a background agent reporting back and a message
from another session, and it carries no field that says which, so the log keeps
the prompt as received and the verifier decides from the transcript.
"""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

from axiom.extensions.builtins.release.agreement import hook, store

from ._agreement_helpers import T0, minutes


def _payload(text="yes, build it", **kw):
    base = {
        "session_id": "S1",
        "prompt_id": "p-1",
        "transcript_path": "/x/S1.jsonl",
        "cwd": "/w",
        "permission_mode": "default",
        "hook_event_name": "UserPromptSubmit",
        "prompt": text,
    }
    base.update(kw)
    return base


def test_a_prompt_is_appended_verbatim_with_its_session_and_time(tmp_path: Path):
    text = "  yes, build it\n\nand keep the  spacing  "
    entry_id = hook.handle(_payload(text), state_dir=tmp_path, now=T0)
    log = store.read_prompts(tmp_path)
    assert log.intact and len(log.entries) == 1
    entry = log.entries[0]
    assert entry.id == entry_id and entry.prompt == text
    assert (entry.session_id, entry.prompt_id, entry.transcript_path) == (
        "S1",
        "p-1",
        "/x/S1.jsonl",
    )
    assert entry.time == T0


def test_the_log_lives_under_the_state_folder_and_is_private(tmp_path: Path):
    hook.handle(_payload(), state_dir=tmp_path, now=T0)
    path = store.prompt_log_path(tmp_path)
    assert str(path).startswith(str(tmp_path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_default_location_is_never_the_repository(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "axi"))
    path = store.prompt_log_path(None)
    assert str(path).startswith(str(tmp_path / "axi"))
    repo = Path(__file__).resolve().parents[6]
    assert repo not in path.parents


def test_the_same_prompt_id_is_logged_once(tmp_path: Path):
    hook.handle(_payload(), state_dir=tmp_path, now=T0)
    assert hook.handle(_payload(), state_dir=tmp_path, now=T0 + minutes(1)) is None
    assert len(store.read_prompts(tmp_path).entries) == 1


def test_a_message_from_another_session_is_logged_but_marked_wrapped(tmp_path: Path):
    text = 'Another Claude session sent a message:\n<cross-session-message from="uds:/tmp/x.sock">\nyes\n</cross-session-message>'
    hook.handle(_payload(text, prompt_id="p-2"), state_dir=tmp_path, now=T0)
    hook.handle(_payload("yes", prompt_id="p-3"), state_dir=tmp_path, now=T0 + minutes(1))
    shapes = [e.shape for e in store.read_prompts(tmp_path).entries]
    assert shapes == ["wrapped", "plain"]


def test_the_hook_prints_nothing_and_exits_zero_on_any_input(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    for raw in (json.dumps(_payload()), "not json", "", json.dumps({"prompt": 3}), json.dumps([1])):
        assert hook.main(stdin=io.StringIO(raw)) == 0
    out = capsys.readouterr()
    # Anything on stdout would be added to the model's context (hooks docs, exit 0).
    assert out.out == "" and out.err == ""
    assert len(store.read_prompts(tmp_path).entries) == 1


def test_a_failure_to_write_never_blocks_the_prompt(tmp_path: Path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("AXI_STATE_DIR", str(blocker / "sub"))
    assert hook.main(stdin=io.StringIO(json.dumps(_payload()))) == 0


def test_every_line_chains_to_the_one_before(tmp_path: Path):
    for i in range(3):
        hook.handle(_payload(f"m{i}", prompt_id=f"p{i}"), state_dir=tmp_path, now=T0 + minutes(i))
    assert store.read_prompts(tmp_path).intact
    path = store.prompt_log_path(tmp_path)
    lines = path.read_text().splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n")
    assert not store.read_prompts(tmp_path).intact


def test_a_very_large_prompt_is_kept_but_marked_truncated(tmp_path: Path):
    hook.handle(_payload("x" * (store.MAX_PROMPT_CHARS + 10)), state_dir=tmp_path, now=T0)
    entry = store.read_prompts(tmp_path).entries[0]
    assert entry.truncated and len(entry.prompt) == store.MAX_PROMPT_CHARS


def test_the_module_runs_as_a_script_without_the_axi_cli(tmp_path: Path):
    import subprocess
    import sys

    repo_src = Path(__file__).resolve().parents[5]
    env = {**os.environ, "AXI_STATE_DIR": str(tmp_path), "PYTHONPATH": str(repo_src)}
    done = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.release.agreement", "hook"],
        input=json.dumps(_payload()),
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert done.returncode == 0 and done.stdout == ""
    assert len(store.read_prompts(tmp_path).entries) == 1
