# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Video is the first instrument kind: ``<cli> instrument video <verb>`` (ADR-256 D4, spec-video §CLI).

Video declares ``[[instrument_kinds]] name = "video"`` and drops its own ``cmd``
noun. It reaches the noun through the same door a consumer's kind uses (the
manifest block, behind the portfolio gate), its verbs and skills are unchanged
(``video.add``; ADR-256 D5), and ``instrument list`` and ``status`` summarise its
sources. Nothing shipped under ``axi video``, so there is no alias.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from axiom.extensions.builtins.instrument import registry
from axiom.extensions.builtins.instrument.commands import cli as instrument_cli
from axiom.extensions.builtins.instrument.kinds import InstrumentKind, InstrumentRow, KindStatus
from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video.commands import cli as video_cli
from axiom.infra import branding


@pytest.fixture
def real_kinds(monkeypatch):
    """This process's catalogue as discovery finds it: the real manifests, the real gate."""
    monkeypatch.setattr(registry, "_CACHE", None)
    yield registry.catalog(refresh=True)
    monkeypatch.setattr(registry, "_CACHE", None)


@pytest.fixture
def acme():
    branding.register(branding.BrandingConfig(cli_name="acme", product_name="Acme"))
    try:
        yield
    finally:
        branding.reset()


def _declare(state_dir: Path, name: str = "cam-1") -> None:
    vc.connect(
        vc.VideoConnector(name=name, source={"kind": "rtsp", "url": "rtsp://h/ok"}),
        state_dir=state_dir,
    )


# ---- the kind ------------------------------------------------------------------


def test_the_kind_is_an_instrument_kind_that_yields_frames():
    from axiom.extensions.builtins.video.instrument import KIND

    assert isinstance(KIND, InstrumentKind)
    assert KIND.name == "video" and KIND.yields == "frames"
    assert KIND.description


def test_its_verbs_are_the_video_skills():
    from axiom.extensions.builtins.video.instrument import KIND

    assert dict(KIND.skill_for_verb) == {v: f"video.{v}" for v in video_cli.VERBS}


def test_discovery_finds_it_in_the_builtin_manifest(real_kinds):
    from axiom.extensions.builtins.video.instrument import KIND

    assert "video" in real_kinds.names()
    assert real_kinds.kind("video") is KIND
    (row,) = [r for r in real_kinds.rows() if r["name"] == "video"]
    assert row["state"] == "registered" and row["origin"] == "builtin"
    assert row["yields"] == "frames"


# ---- the command line ------------------------------------------------------------


def test_instrument_video_reaches_the_video_verbs(real_kinds, state_dir, capsys):
    _declare(state_dir)
    assert instrument_cli.main(["video", "list", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert [s["name"] for s in envelope["value"]["sources"]] == ["cam-1"]


def test_the_usage_names_the_full_command(real_kinds, capsys):
    with pytest.raises(SystemExit) as exc:
        instrument_cli.main(["video", "--help"])
    assert exc.value.code == 0
    assert "usage: axi instrument video" in capsys.readouterr().out


def test_under_a_brand_the_usage_names_the_brands_command(real_kinds, acme, capsys):
    with pytest.raises(SystemExit):
        instrument_cli.main(["video", "add", "--help"])
    out = capsys.readouterr().out
    assert "usage: acme instrument video add" in out


def test_run_directly_the_video_cli_names_the_same_command(capsys):
    with pytest.raises(SystemExit):
        video_cli.main(["--help"])
    assert "usage: axi instrument video" in capsys.readouterr().out


def test_an_empty_list_says_how_to_declare_one(real_kinds, state_dir, capsys):
    assert instrument_cli.main(["video", "list"]) == 0
    out = capsys.readouterr().out
    assert "axi instrument video add" in out and "axi video" not in out


# ---- what the noun summarises ----------------------------------------------------


def test_instrument_list_has_a_row_per_video_source(real_kinds, state_dir):
    from axiom.extensions.builtins.video.instrument import KIND

    _declare(state_dir, "cam-1")
    _declare(state_dir, "cam-2")
    rows = KIND.list()
    assert all(isinstance(r, InstrumentRow) for r in rows)
    assert [(r.kind, r.name, r.health, r.state) for r in rows] == [
        ("video", "cam-1", "unmounted", "stopped"),
        ("video", "cam-2", "unmounted", "stopped"),
    ]
    assert rows[0].detail.startswith("rtsp")


def test_instrument_status_counts_the_sources_by_health(real_kinds, state_dir):
    from axiom.extensions.builtins.video.instrument import KIND

    _declare(state_dir, "cam-1")
    summary = KIND.status()
    assert isinstance(summary, KindStatus)
    assert summary.ok is True and summary.kind == "video"
    assert dict(summary.counts) == {"unmounted": 1}
    assert summary.summary.startswith("1 source")
    assert summary.fix and "axi instrument video" in summary.fix


def test_with_no_sources_status_says_so(real_kinds, state_dir):
    from axiom.extensions.builtins.video.instrument import KIND

    summary = KIND.status()
    assert summary.ok is True and dict(summary.counts) == {}
    assert "no sources" in summary.summary


def test_the_nouns_own_list_and_status_include_video(real_kinds, state_dir, capsys):
    _declare(state_dir)
    assert instrument_cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "video" in out and "cam-1" in out and "unmounted" in out
    assert instrument_cli.main(["list", "--kinds"]) == 0
    assert "frames" in capsys.readouterr().out
    assert instrument_cli.main(["status"]) == 0
    assert "video: 1 source" in capsys.readouterr().out


# ---- help, Tab and next: ------------------------------------------------------


def _doc():
    from axiom import axiom_cli
    from axiom.cli import command_space as cs
    from axiom.cli import completion_index as ci
    from axiom.infra.skills import SkillRegistry

    top = argparse.ArgumentParser(prog="axi")
    sub = top.add_subparsers(dest="subcommand")
    noun = instrument_cli.get_parser()
    node = sub.add_parser("instrument", help="Instruments", description=noun.description)
    axiom_cli._copy_subparsers(noun, node)
    from axiom.extensions.builtins.instrument import skills as instrument_skills
    from axiom.extensions.builtins.video import skills as video_skills

    reg = SkillRegistry()
    video_skills.bind(reg)
    instrument_skills.bind(reg)
    maps = {"instrument": dict(instrument_cli.SKILL_FOR_VERB)}
    return cs.annotate(ci.build_doc(top, {}), reg.specs(), maps=maps)


def test_tab_reaches_the_video_verbs_with_their_skills(real_kinds):
    tree = _doc()["commands"][0]["tree"]
    add = tree["subs"]["video"]["subs"]["add"]
    flags = {n for a in add["args"] for n in a["names"]}
    assert {"--kind", "--url", "--credential-ref", "--transport", "--fps-cap"} <= flags
    assert add["skill"] == "video.add" and add["risk"] == "reversible-write"
    status = tree["subs"]["video"]["subs"]["status"]
    assert status["skill"] == "video.status" and status["risk"] == "read"


def test_next_lines_offer_the_video_reads(real_kinds):
    from axiom.cli import command_space as cs
    from axiom.cli import next_steps

    space = cs.from_doc(_doc())
    lines = next_steps.suggest(("instrument", "video"), space=space, cache=None, recent=[], limit=5)
    assert any(line.startswith("axi instrument video status") for line in lines), lines


@pytest.mark.parametrize("argv", [["--help"], ["list", "--kinds"], ["video", "--help"]])
def test_the_noun_with_video_registered_imports_no_video_library(argv, tmp_path):
    code = (
        "import sys\n"
        "from axiom.extensions.builtins.instrument.commands import cli\n"
        "try:\n"
        f"    cli.main({argv!r})\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('HEAVY', sorted(m for m in ('numpy', 'cv2', 'av') if m in sys.modules))\n"
    )
    env = {**os.environ, "AXI_STATE_DIR": str(tmp_path / "state"), "AXI_NO_SOURCE_WARNING": "1"}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert "video" in out.stdout
    assert "HEAVY []" in out.stdout, out.stdout + out.stderr


# ---- through the platform's own dispatcher ---------------------------------------


def _axi(monkeypatch, state_dir: Path, *argv: str) -> int:
    from axiom import axiom_cli

    monkeypatch.setattr(sys, "argv", ["axi", *argv])
    try:
        rc = axiom_cli.main()
    except SystemExit as exc:
        rc = exc.code
    return rc or 0


def test_axi_instrument_video_runs_through_the_top_level_command(
    real_kinds, monkeypatch, state_dir, capsys
):
    _declare(state_dir)
    assert _axi(monkeypatch, state_dir, "instrument", "video", "list", "--json") == 0
    envelope = json.loads(capsys.readouterr().out)
    assert [s["name"] for s in envelope["value"]["sources"]] == ["cam-1"]


def test_axi_video_is_not_a_noun(monkeypatch, state_dir, capsys):
    """Nothing shipped under ``axi video``, so there is no alias to keep (ADR-256)."""
    assert _axi(monkeypatch, state_dir, "video", "list") == 1
    assert "unknown subcommand 'video'" in capsys.readouterr().out
