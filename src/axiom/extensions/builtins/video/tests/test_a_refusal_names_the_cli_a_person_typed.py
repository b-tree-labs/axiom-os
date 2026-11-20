# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Under a consumer's distribution, every fix ``video`` gives names that distribution's CLI,
and the command it names is ``<cli> instrument video <verb>`` (ADR-256 D4).

Found by running ``neut video start`` on a source that had not been verified:
the refusal said to run ``axi video verify``, a command that was not on that
operator's PATH. The precondition fixes were literal strings; the repo's
branding guard reads printed text and argparse help, never a ``Precondition``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from axiom.extensions.builtins.video import connector as vc
from axiom.infra import branding

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def acme():
    branding.register(branding.BrandingConfig(cli_name="acme", product_name="Acme"))
    try:
        yield
    finally:
        branding.reset()


def _text(result) -> str:
    return " ".join(result.errors) + " " + " ".join(u["fix"] for u in result.unmet)


def test_starting_an_undeclared_source_names_the_consumers_cli(acme, call, state_dir):
    text = _text(call("video.start", name="ghost"))
    assert "acme instrument video add ghost" in text
    assert "axi video" not in text and "acme video " not in text


def test_starting_an_unverified_source_names_the_consumers_cli(acme, call, state_dir):
    vc.connect(
        vc.VideoConnector(name="cam-1", source={"kind": "rtsp", "url": "rtsp://h/ok"}),
        state_dir=state_dir,
    )
    text = _text(call("video.start", name="cam-1"))
    assert "acme instrument video verify cam-1" in text
    assert "acme instrument video diagnose cam-1" in text
    assert "axi video" not in text and "acme video " not in text


def test_a_login_in_a_url_names_the_consumers_vault(acme, call, state_dir):
    text = _text(call("video.add", name="cam-1", kind="rtsp", url="rtsp://u:p@h/s"))
    assert "acme vault set video-cam-1" in text and "axi vault" not in text


def _fix_literals() -> list[tuple[str, str]]:
    """Every string handed to ``Precondition(fix=...)`` in the extension's shipped code."""
    found = []
    for path in ROOT.rglob("*.py"):
        if "tests" in path.relative_to(ROOT).parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "fix":
                for const in ast.walk(node.value):
                    if isinstance(const, ast.Constant) and isinstance(const.value, str):
                        found.append((path.name, const.value))
    return found


def test_no_fix_hardcodes_the_platform_cli():
    literals = _fix_literals()
    assert literals, "the scan found no fixes, so it proves nothing"
    offenders = [(f, s) for f, s in literals if "axi " in s]
    assert not offenders, f"write {{cli}} instead of axi: {offenders}"


def test_the_capture_process_names_the_cli_that_started_it(state_dir, fake_ffmpeg):
    """A fix the capture service writes (a trigger that cannot be attached, a login that does
    not resolve) is read by a person who typed their product's command. The capture process
    is started by that command, so it names it, not the platform's own."""
    import json
    import subprocess
    import sys

    from axiom.extensions.builtins.video import connector as vc
    from axiom.extensions.builtins.video import state as vs

    from .harness import child_env, stop_everything, until

    vc.connect(
        vc.VideoConnector(
            name="cam-1",
            source={"kind": "rtsp", "url": "rtsp://127.0.0.1:8554/ok"},
            triggers=({"name": "motoin"},),
        ),
        state_dir=state_dir,
    )
    assert vc.verify("cam-1", state_dir=state_dir).ok
    driver = (
        "import sys\n"
        "from axiom.infra import branding\n"
        "branding.register(branding.BrandingConfig(cli_name='acme', product_name='Acme'))\n"
        "from axiom.extensions.builtins.video.commands.cli import main\n"
        "sys.exit(main(['start', 'cam-1']))\n"
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", driver],
            env=child_env(state_dir, fake_ffmpeg.env()),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        failed = until(
            lambda: [
                t
                for t in (vs.read_live("cam-1", state_dir) or {}).get("triggers") or []
                if t.get("fix")
            ]
        )
        assert "acme instrument video list --triggers" in failed[0]["fix"], json.dumps(failed)
        seen = json.dumps(vs.read_live("cam-1", state_dir))
        assert "axi video" not in seen and "acme video " not in seen
    finally:
        stop_everything(state_dir)


def test_the_boot_unit_carries_the_cli_that_installed_it(state_dir, monkeypatch):
    from axiom.extensions.builtins.video import boot
    from axiom.infra import branding

    branding.register(branding.BrandingConfig(cli_name="acme", product_name="Acme"))
    try:
        spec = boot.service_definition(state_dir)
    finally:
        branding.reset()
    assert spec["env"]["AXIOM_VIDEO_CLI"] == "acme"
#: A video verb named without the noun it now sits under.
_OLD_COMMAND = re.compile(
    r"(?<!instrument )\bvideo (add|verify|list|status|start|stop|diagnose|install|uninstall|revoke|events)\b"
)


def _shipped_text() -> list[tuple[str, str]]:
    """Every shipped file a person's text comes from: code, the manifest, each SKILL.md."""
    found = []
    for path in [*ROOT.rglob("*.py"), *ROOT.rglob("*.toml"), *ROOT.rglob("SKILL.md")]:
        if "tests" in path.relative_to(ROOT).parts or "__pycache__" in path.parts:
            continue
        found.append((str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")))
    return found


def test_no_shipped_text_names_a_video_verb_without_instrument():
    """Nothing shipped under ``axi video``, so every mention moves (ADR-256 D4)."""
    shipped = _shipped_text()
    assert len(shipped) > 20, "the scan found too little to prove anything"
    offenders = [
        (name, m.group(0))
        for name, text in shipped
        for m in _OLD_COMMAND.finditer(text)
    ]
    assert not offenders, f"write `<cli> instrument video <verb>`: {offenders}"


def test_the_guard_can_fail():
    assert _OLD_COMMAND.search("run `{cli} video verify cam-1`")
    assert not _OLD_COMMAND.search("run `{cli} instrument video verify cam-1`")
