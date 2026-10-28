# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Kit automations, everything that needs no database.

An automation is a declaration (``automations/<name>.toml``): run one of the
kit's verbs on a cadence and, when a value crosses a line, notify the submitter
or annotate the data. ``kit-check`` is where a bad one is caught, the scaffold
writes one, and the bundle carries it (and refuses code beside it).
"""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit import automations as auto
from axiom.extensions.builtins.data_platform.kit import declarations as decl
from axiom.extensions.builtins.data_platform.kit.bundle import DECLARATION_DIRS, pack, unpack
from axiom.extensions.builtins.data_platform.kit.check import check_kit
from axiom.extensions.builtins.data_platform.kit.project import KitError

GOOD = """description = "Tell me when the daily rise runs hot."
every = "1h"
verb = "temperature_rise_by_day"
params = { since = "2026-09-01" }

[when]
column = "rise"
above = 30.0

[then]
notify = "submitter"
annotate = { label = "hot", note = "rise above 30" }
"""


@pytest.fixture
def project(tmp_path: Path):
    kit.init(tmp_path, "rig-site")
    return kit.load(tmp_path)


def _write(project, name: str, text: str) -> None:
    folder = project.root / "automations"
    folder.mkdir(exist_ok=True)
    (folder / f"{name}.toml").write_text(text, encoding="utf-8")


def _problems(project) -> list[str]:
    return [p for p in decl.load(project).problems if "automations/" in p]


def test_a_well_formed_automation_loads_and_passes_the_check(project):
    _write(project, "hot_rise", GOOD)
    ds = decl.load(project)
    assert ds.problems == []
    (a,) = ds.automations
    assert (a.name, a.verb, a.every_seconds, a.cron) == ("hot_rise", "temperature_rise_by_day", 3600, None)
    assert a.when.column == "rise" and a.when.above == 30.0
    assert a.notify and a.annotate == {"label": "hot", "note": "rise above 30"}
    assert check_kit(project, isolation=False)["declarations"] == []


@pytest.mark.parametrize(
    ("edit", "says"),
    [
        (lambda t: t.replace('description = "Tell me when the daily rise runs hot."', 'description = ""'),
         "add a description"),
        (lambda t: t.replace('verb = "temperature_rise_by_day"', 'verb = "no_such_verb"'),
         "is not a verb in this kit"),
        (lambda t: t.replace('params = { since = "2026-09-01" }', "params = { site = 'other' }"),
         "has no parameter"),
        (lambda t: t.replace('column = "rise"', 'column = "nonsense_col"'),
         "does not appear in"),
        (lambda t: t.replace('every = "1h"', 'every = "1m"'), "at least 5 minutes"),
        (lambda t: t.replace('every = "1h"', 'every = "hourly"'), "every ="),
        (lambda t: t.replace('every = "1h"', 'every = "1h"\ncron = "0 7 * * 1-5"'), "one of every or cron"),
        (lambda t: t.replace('every = "1h"\n', ""), "one of every or cron"),
        (lambda t: t.replace('every = "1h"', 'cron = "0 7 * *"'), "cron"),
        (lambda t: t.replace('every = "1h"', 'cron = "*/2 * * * *"'), "at least 5 minutes"),
        (lambda t: t.split("[then]")[0] + "[then]\n", "at least one action"),
        (lambda t: t.replace('notify = "submitter"', 'notify = "everyone"'), 'notify = "submitter"'),
        (lambda t: t.replace("above = 30.0", ""), "above, below or any_rows"),
        (lambda t: t.replace("above = 30.0", "above = 30.0\nbelow = 2.0"), "above, below or any_rows"),
        (lambda t: t.replace('label = "hot"', 'label = ""'), "label"),
        (lambda t: t.replace("[when]", "colour = 'red'\n[when]"), "colour"),
    ],
)
def test_kit_check_refuses_a_bad_automation_naming_the_file(project, edit, says):
    _write(project, "hot_rise", edit(GOOD))
    problems = _problems(project)
    assert problems, f"expected a problem mentioning {says!r}"
    assert all(p.startswith("data/automations/hot_rise.toml") for p in problems), problems
    assert any(says in p for p in problems), problems
    assert check_kit(project, isolation=False)["ok"] is False


def test_any_rows_needs_no_column_and_no_when_acts_on_every_run(project):
    _write(project, "any", GOOD.replace('column = "rise"\nabove = 30.0', "any_rows = true"))
    _write(project, "always", GOOD.split("[when]")[0] + '[then]\nnotify = "submitter"\n')
    ds = decl.load(project)
    assert ds.problems == [], ds.problems
    by = {a.name: a for a in ds.automations}
    assert by["any"].when.any_rows and by["always"].when is None


def test_when_selects_the_rows_that_crossed_the_line():
    a = auto.Automation(name="hot", description="d", verb="v", params={}, every_seconds=3600, cron=None,
                        when=auto.When(column="mean", above=30.0), notify=True, annotate=None)
    rows = [{"mean": 29.0}, {"mean": 31.5}, {"mean": None}, {"mean": "32"}]
    assert auto.matching_rows(a, rows) == [{"mean": 31.5}, {"mean": "32"}]
    below = auto.Automation(**{**a.__dict__, "when": auto.When(column="mean", below=30.0)})
    assert auto.matching_rows(below, rows) == [{"mean": 29.0}]
    anyrows = auto.Automation(**{**a.__dict__, "when": auto.When(any_rows=True)})
    assert auto.matching_rows(anyrows, rows) == rows and auto.matching_rows(anyrows, []) == []
    with pytest.raises(KitError, match="no column 'mean'"):
        auto.matching_rows(a, [{"other": 1}])


def test_the_message_names_the_kit_the_automation_the_rows_and_how_to_stop_it():
    a = auto.Automation(name="hot", description="d", verb="rise_by_day", params={}, every_seconds=3600,
                        cron=None, when=auto.When(column="mean", above=30.0), notify=True, annotate=None)
    rows = [{"day": f"2026-09-{i:02d}", "mean": 30 + i} for i in range(1, 15)]
    subject, body = auto.message("rig-site", a, rows, total=20)
    assert "rig-site" in subject and "hot" in subject and "14" in subject
    assert "mean above 30" in body and "day=2026-09-01" in body
    assert "… and 4 more" in body, "the rows are capped"
    assert "kit-retire rig-site" in body and "automations/hot.toml" in body


def test_kit_scaffold_writes_an_automation_that_only_needs_its_description(project):
    written = kit.scaffold_contribution(project, "automation", "hot_rise", verb="temperature_rise_by_day",
                                        every="1h", above=30.0, column="rise", notify=True, annotate="hot")
    assert written == ["automations/hot_rise.toml"]
    problems = _problems(project)
    assert len(problems) == 1 and "add a description" in problems[0], problems
    path = project.root / "automations" / "hot_rise.toml"
    path.write_text(path.read_text().replace('description = ""', 'description = "Hot days."'))
    ds = decl.load(project)
    assert ds.problems == [], ds.problems
    (a,) = ds.automations
    assert a.when.above == 30.0 and a.notify and a.annotate["label"] == "hot"


def test_kit_scaffold_automation_refuses_what_check_would(project):
    with pytest.raises(KitError, match="is not a verb in this kit"):
        kit.scaffold_contribution(project, "automation", "x", verb="nope", every="1h", notify=True)
    with pytest.raises(KitError, match="at least one action"):
        kit.scaffold_contribution(project, "automation", "x", verb="temperature_rise_by_day", every="1h")
    with pytest.raises(KitError, match="one of --every or --cron"):
        kit.scaffold_contribution(project, "automation", "x", verb="temperature_rise_by_day", notify=True)
    assert not (project.root / "automations" / "x.toml").exists()


def test_kit_scaffold_automation_through_the_cli(project, capsys):
    from axiom.extensions.builtins.data_platform.commands.cli import main

    rc = main(["kit-scaffold", "automation", "cold", "--dir", str(project.root.parent),
               "--verb", "temperature_rise_by_day", "--cron", "0 7 * * 1-5", "--below", "2",
               "--column", "rise", "--notify"])
    assert rc == 0, capsys.readouterr()
    text = (project.root / "automations" / "cold.toml").read_text()
    assert 'cron = "0 7 * * 1-5"' in text and "below = 2.0" in text and 'notify = "submitter"' in text


def test_the_bundle_carries_automations_and_refuses_code_in_them(project):
    assert "automations" in DECLARATION_DIRS
    _write(project, "hot_rise", GOOD)
    b = pack(project)
    assert "automations/hot_rise.toml" in b.files
    (project.root / "automations" / "sneaky.py").write_text("import os\n")
    with pytest.raises(KitError, match="automations/sneaky.py"):
        pack(project)


def _tar(members: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return gzip.compress(raw.getvalue())


def test_unpack_refuses_code_under_automations(tmp_path):
    ok = _tar({"kit.toml": b'[kit]\ntenant = "rig-site"\n', "automations/a.toml": b"x = 1\n"})
    assert (unpack(ok, tmp_path / "ok") / "automations" / "a.toml").is_file()
    bad = _tar({"kit.toml": b'[kit]\ntenant = "rig-site"\n', "automations/a.py": b"import os\n"})
    with pytest.raises(KitError, match="automations/a.py"):
        unpack(bad, tmp_path / "bad")


def test_the_node_rejects_a_bundle_whose_automation_is_bad(project):
    from axiom.extensions.builtins.data_platform.kit import host

    _write(project, "hot_rise", GOOD.replace('verb = "temperature_rise_by_day"', 'verb = "gone"'))
    problems = host.static_problems(project, bundle_has_code=False)
    assert any("automations/hot_rise.toml" in p and "is not a verb" in p for p in problems)


def test_an_automation_becomes_a_pulse_cadence():
    from datetime import timedelta

    a = auto.Automation(name="h", description="d", verb="v", params={}, every_seconds=3600, cron=None,
                        when=None, notify=True, annotate=None)
    assert a.cadence().kind == "interval" and a.cadence().interval == timedelta(hours=1)
    c = auto.Automation(**{**a.__dict__, "every_seconds": None, "cron": "0 7 * * 1-5"})
    assert c.cadence().kind == "cron" and c.cadence().cron == "0 7 * * 1-5"
    assert auto.Automation.from_record("h", a.to_record()) == a
    env = auto.envelope("rig-site", "h")
    assert env["params"] == {"site": "rig-site", "automation": "h"}
    assert env["owner"] == "@kit_rig_site:rig-site" and env["role"] == "kit_rig_site"
