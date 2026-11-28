# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi data readings-hypertable`` reaches the ``data.readings_hypertable`` skill.

The migration's docstring, its SKILL.md and the operator runbook all tell a
person to run ``axi data readings-hypertable <phase>``; the skill was
registered but no verb was, so the command a runbook names failed with
"invalid choice". Found rehearsing the runbook.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform import cli


def test_the_verb_exists_and_maps_to_the_skill():
    args = cli._parser().parse_args(["readings-hypertable"])
    assert cli.resolve_skill(args)[0] == "readings_hypertable"
    assert cli._args_to_params(args)["phase"] == "estimate", "estimate (writes nothing) is the default"


def test_every_flag_reaches_the_skill_under_its_own_name():
    args = cli._parser().parse_args([
        "readings-hypertable", "swap", "--confirm", "--backup-file", "/data/s.dump",
        "--conform-paused", "--max-windows", "3", "--compress-after-days", "14", "--dsn", "postgresql:///x",
    ])
    p = cli._args_to_params(args)
    assert p["phase"] == "swap" and p["confirm"] is True and p["conform_paused"] is True
    assert p["backup_file"] == "/data/s.dump" and p["max_windows"] == 3 and p["compress_after_days"] == 14
    assert "accept_rederive" not in p, "an unset flag is not sent: the skill's own default applies"


def test_the_rederive_path_is_explicit():
    p = cli._args_to_params(cli._parser().parse_args(["readings-hypertable", "prepare", "--confirm",
                                                       "--accept-rederive"]))
    assert p["accept_rederive"] is True and "backup_file" not in p
