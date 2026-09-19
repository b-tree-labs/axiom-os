# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi data backup-policy`` — the step between editing a policy and arming it.

Arming the nightly backup is done by editing the policy file. So the arming
gesture and the configuration are the same gesture, and there was no point at
which anyone looked at what the node had actually loaded. Two different bugs
lived in that gap:

  1. the loader was behind the dataclass, so `exclude_table_data` and
     `timeout_s` were set in the file and dropped on the way in
  2. a file whose settings sit at top level instead of under
     ``[backup_policy]`` parses fine, loads an empty table, and yields pure
     defaults — so ``enabled = true`` written that way arms nothing

Both look identical from the file. This verb reports the LOADED policy and
names anything the file said that did not survive.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.data_platform import skills as data_skills
from axiom.extensions.builtins.data_platform.skills import backup_policy
from axiom.infra.skills import SkillContext


def _run(tmp_path, text: str | None, *, sched=("not armed", [], [])):
    """Run the verb with the PULSE read stubbed.

    Most of these tests are about the POLICY half and must not depend on a
    migrated local database. `sched` is `(verdict, lines, problems)`; the
    schedule half is exercised explicitly through `_with_rows` below.
    """
    from unittest import mock

    if text is not None:
        (tmp_path / "plinth").mkdir(parents=True, exist_ok=True)
        (tmp_path / "plinth" / "backup_policy.toml").write_text(text, encoding="utf-8")
    ctx = SkillContext(
        registry=data_skills.bind_default(),
        state_dir=tmp_path,
        logger=logging.getLogger("test"),
        user_prompt=None,
    )
    verdict, lines, problems = sched
    with mock.patch.object(backup_policy, "_scheduled", lambda sd: (lines, problems, verdict)):
        return backup_policy.run({"state_dir": str(tmp_path)}, ctx)


GOOD = """
[backup_policy]
enabled = false
schedule = "0 2 * * *"
retention_count = 7
timeout_s = 3600
exclude_table_data = ["public.reactor_timeseries_default", "public.chunks", "silver.signals"]
offbox = "none"
"""


def test_it_reports_what_the_node_loaded_not_what_the_file_says():
    """The distinction the whole verb exists for."""
    # Not a fixture: this is the shape of the file actually on the node.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        result = _run(Path(d), GOOD)
        assert result.ok, result.errors
        policy = result.value["policy"]
        assert policy["timeout_s"] == 3600, "the field that was silently dropped"
        assert policy["exclude_table_data"] == [
            "public.reactor_timeseries_default",
            "public.chunks",
            "silver.signals",
        ]
        assert policy["retention_count"] == 7


def test_a_headerless_file_is_called_out_rather_than_read_as_defaults(tmp_path):
    """`enabled = true` at top level arms nothing and looks armed."""
    result = _run(tmp_path, 'enabled = true\nschedule = "30 2 * * *"\n')
    assert not result.ok
    joined = " ".join(result.errors)
    assert "[backup_policy]" in joined
    assert "enabled" in joined
    assert result.value["policy"]["enabled"] is False


def test_a_misspelled_key_is_named(tmp_path):
    result = _run(tmp_path, GOOD + '\nexcluded_tables = ["typo"]\n')
    assert not result.ok
    assert "excluded_tables" in " ".join(result.errors)


def test_no_policy_says_backups_are_not_scheduled(tmp_path):
    """Absent is a legitimate state, and must not read as 'fine'."""
    result = _run(tmp_path, None)
    assert result.ok
    assert result.value["exists"] is False
    assert any("not scheduled" in a.lower() for a in result.actions_taken)


def test_it_says_plainly_what_the_policy_asks_for_and_what_pulse_does(tmp_path):
    """Two separate facts, reported separately. Conflating them is the bug."""
    off = _run(tmp_path, GOOD)
    assert any("POLICY ASKS FOR: nothing" in a for a in off.actions_taken)

    on = _with_rows(tmp_path, GOOD.replace("enabled = false", "enabled = true"),
                    {"data.backup": "active", "data.backup_validate": "active"})
    asks = [a for a in on.actions_taken if a.startswith("POLICY ASKS FOR")]
    assert asks and "data.backup" in asks[0], "name the skills, not just 'on'"
    assert any("PULSE SAYS: armed" in a for a in on.actions_taken)


@pytest.mark.parametrize("field", ["exclude_table_data", "timeout_s", "validate_restore"])
def test_every_field_is_shown_including_the_ones_that_were_dropped(tmp_path, field):
    """If a field is not displayed, this verb cannot catch the next drop."""
    result = _run(tmp_path, GOOD)
    assert any(field in a for a in result.actions_taken)


# ---------------------------------------------------------------------------
# The policy is an intention. A PULSE row is the effect.
#
# `ensure_backup_cadences` runs once, at orchestrator startup. Nothing re-runs
# it when the policy file changes. So writing `enabled = true` arms nothing
# until the service restarts — and there was no way to tell, because the file
# and the schedule can disagree with no symptom anywhere.
#
# "I armed the backup" is exactly the kind of claim that has been wrong all
# week. These check that the verb reads the rows and says so.
# ---------------------------------------------------------------------------

from unittest import mock  # noqa: E402


def _with_rows(tmp_path, text, rows):
    """`rows` is {action: state|None}; derive the verdict the way the real reader does."""
    lines, active = [], 0
    for action, state in rows.items():
        if state is None:
            lines.append(f"   {action:<22} NO SCHEDULE ROW — will not run")
            continue
        lines.append(f"   {action:<22} {state}")
        if state == "active":
            active += 1
    verdict = "armed" if active == 2 else ("partial" if active else "not armed")
    return _run(tmp_path, text, sched=(verdict, lines, []))


ARMED = GOOD.replace("enabled = false", "enabled = true")
BOTH = ["data.backup", "data.backup_validate"]


def test_enabled_with_no_schedule_rows_is_an_error_not_a_green_report(tmp_path):
    """The failure this verb exists for: armed in the file, armed nowhere else."""
    r = _with_rows(tmp_path, ARMED, dict.fromkeys(BOTH, None))
    assert not r.ok
    joined = " ".join(r.errors)
    assert "NOTHING is scheduled" in joined
    assert "--apply" in joined, "say how to fix it, in the message that reports it"
    assert r.value["pulse"] == "not armed"


def test_enabled_with_both_rows_active_reports_armed(tmp_path):
    r = _with_rows(tmp_path, ARMED, dict.fromkeys(BOTH, "active"))
    assert r.ok, r.errors
    assert r.value["pulse"] == "armed"
    assert any("PULSE SAYS: armed" in a for a in r.actions_taken)


def test_one_cadence_active_is_called_out(tmp_path):
    """A backup with no validation is the state that produced an 83 MB dump
    nobody could restore. Half-armed must not read as armed."""
    r = _with_rows(tmp_path, ARMED, {"data.backup": "active", "data.backup_validate": None})
    assert not r.ok
    assert "only one of the two cadences" in " ".join(r.errors)
    assert r.value["pulse"] == "partial"


def test_disabled_but_rows_still_active_is_an_error(tmp_path):
    """A disable that has not taken effect is as misleading as an arm that has not."""
    r = _with_rows(tmp_path, GOOD, dict.fromkeys(BOTH, "active"))
    assert not r.ok
    assert "disabled but PULSE rows are still active" in " ".join(r.errors)


def test_disabled_with_no_rows_is_the_quiet_correct_state(tmp_path):
    r = _with_rows(tmp_path, GOOD, dict.fromkeys(BOTH, None))
    assert r.ok, r.errors
    assert r.value["pulse"] == "not armed"


def test_an_unreadable_schedule_store_is_unknown_not_not_armed(tmp_path):
    """Claiming 'not armed' when the table could not be read is a guess
    presented as a fact — the whole habit this verb is pushing back on."""
    r = _run(
        tmp_path, ARMED, sched=("unknown", [], ["cannot read PULSE schedules: boom"])
    )
    assert r.value["pulse"] == "unknown"
    assert not r.ok
    assert "cannot read PULSE schedules" in " ".join(r.errors)
    # and it must NOT additionally assert the policy is unarmed
    assert "NOTHING is scheduled" not in " ".join(r.errors)


def test_apply_projects_the_policy_and_says_what_it_did(tmp_path):
    from axiom.extensions.builtins.data_platform.skills import backup_policy as bp

    called = {}

    class _Orch:
        def __init__(self, **kw):
            called["ctor"] = kw

        def ensure_backup_cadences(self):
            called["applied"] = True
            return {"enabled": True, "data.backup": "registered:1"}

    def fake(state_dir):
        state = "active" if called.get("applied") else None
        return [], [], "armed" if state else "not armed"

    with (
        mock.patch.object(bp, "_scheduled", fake),
        mock.patch(
            "axiom.extensions.builtins.data_platform.orchestration.service."
            "OrchestratorService",
            _Orch,
        ),
    ):
        (tmp_path / "plinth").mkdir(parents=True, exist_ok=True)
        (tmp_path / "plinth" / "backup_policy.toml").write_text(ARMED, encoding="utf-8")
        ctx = SkillContext(
            registry=data_skills.bind_default(),
            state_dir=tmp_path,
            logger=logging.getLogger("test"),
            user_prompt=None,
        )
        r = backup_policy.run({"state_dir": str(tmp_path), "apply": True}, ctx)

    assert called.get("applied"), "--apply must actually project the policy"
    assert r.value["pulse"] == "armed", "the report must describe the state it leaves"
    assert r.ok


def test_the_schedule_reader_uses_columns_that_exist():
    """A getattr() default over a wrong column name reports 'no next run'
    forever. The node's table is `next_fire_at`; `next_run_at` does not exist.

    Checked against the real model rather than a fixture, because a fixture
    would happily agree with whatever name the reader invented."""
    import inspect

    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition
    from axiom.extensions.builtins.data_platform.skills import backup_policy as bp

    # Comments are stripped: the guard is about what the code reads, and the
    # code carries a comment naming the wrong column precisely to explain it.
    source = "\n".join(
        line.split("#", 1)[0]
        for line in inspect.getsource(bp._scheduled).splitlines()
    )
    real = {c.name for c in ScheduleDefinition.__table__.columns}
    assert "next_fire_at" in real, "the model changed — update this guard, not the reader"
    assert "row.next_fire_at" in source
    assert "next_run_at" not in source, (
        "next_run_at is not a column on schedule_definition; reading it through a "
        "getattr() default yields None and reports 'no next run' every time"
    )


def test_a_dsn_in_the_policy_is_never_printed_in_full(tmp_path):
    """The verb prints the whole policy. A password must not ride along.

    `scratch_db` exists precisely so a disposable database can be named without
    persisting credentials; `scratch_dsn` remains for the cases that need a
    different server, and is redacted wherever it is shown.
    """
    text = GOOD + '\nscratch_dsn = "postgresql://axiom:sup3rsecret@localhost:5432/scratch"\n'
    r = _run(tmp_path, text)
    blob = " ".join(r.actions_taken) + " " + str(r.value)
    assert "sup3rsecret" not in blob
    assert "axiom:***@localhost:5432/scratch" in blob, "redact the password, keep the identity"


def test_scratch_db_is_a_name_and_needs_no_redaction(tmp_path):
    r = _run(tmp_path, GOOD + '\nscratch_db = "axiom_backup_verify"\n')
    assert r.value["policy"]["scratch_db"] == "axiom_backup_verify"
    assert any("axiom_backup_verify" in a for a in r.actions_taken)


def test_the_scratch_dsn_is_derived_from_the_live_one():
    """Same server, same credentials, different database — nothing persisted."""
    from axiom.extensions.builtins.data_platform.skills.backup_validate import scratch_dsn_for

    got = scratch_dsn_for("postgresql://axiom:pw@localhost:5432/axiom_db", "axiom_backup_verify")
    assert got == "postgresql://axiom:pw@localhost:5432/axiom_backup_verify"
    # and it must not silently keep the live database name
    assert not got.endswith("/axiom_db")
