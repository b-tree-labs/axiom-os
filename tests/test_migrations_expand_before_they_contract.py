# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A migration never breaks the release still running beside it (expand/contract).

During a blue/green deploy both releases run against one database, so a new
migration may only add. Dropping or renaming a table or column, or making a
column NOT NULL, is allowed only in a migration that says which released
version stopped using it:

    # contract: <what> unused since <released version>

and that version must already be released (a tag), so the drop ships at least
one release after the code stopped reading it.
"""

from pathlib import Path

import pytest

from axiom.infra.deploy.migration_guard import contracting_operations, guard

ROOT = Path(__file__).resolve().parents[1]


def test_additions_pass(tmp_path):
    m = tmp_path / "0003_add.py"
    m.write_text("def upgrade():\n    op.add_column('t', sa.Column('c', sa.Text(), nullable=True))\n")
    assert guard([m], released={"0.67.4"}) == []


@pytest.mark.parametrize("line", [
    "op.drop_column('t', 'c')",
    "op.drop_table('t')",
    "op.rename_table('t', 'u')",
    "op.alter_column('t', 'c', new_column_name='d')",
    "op.alter_column('t', 'c', nullable=False)",
    "op.execute('ALTER TABLE t DROP COLUMN c')",
    "op.execute('alter table t rename column c to d')",
])
def test_a_contracting_change_without_a_marker_is_refused(tmp_path, line):
    m = tmp_path / "0004_drop.py"
    m.write_text(f"def upgrade():\n    {line}\n")
    assert contracting_operations(m.read_text())
    problems = guard([m], released={"0.67.4"})
    assert problems and "0004_drop.py" in problems[0]


def test_a_contract_after_a_released_version_passes(tmp_path):
    m = tmp_path / "0005_drop.py"
    m.write_text("# contract: t.c unused since 0.67.4\ndef upgrade():\n    op.drop_column('t', 'c')\n")
    assert guard([m], released={"0.67.4"}) == []


def test_a_contract_naming_an_unreleased_version_is_refused(tmp_path):
    m = tmp_path / "0006_drop.py"
    m.write_text("# contract: t.c unused since 0.68.0\ndef upgrade():\n    op.drop_column('t', 'c')\n")
    assert "0.68.0" in guard([m], released={"0.67.4"})[0]


def test_a_downgrade_may_drop_what_its_upgrade_added(tmp_path):
    m = tmp_path / "0007_add.py"
    m.write_text("def upgrade():\n    op.add_column('t', sa.Column('c'))\n\ndef downgrade():\n    op.drop_column('t', 'c')\n")
    assert guard([m], released={"0.67.4"}) == []


# -- What a line-matching guard missed (ported to an AST read of upgrade()) --


@pytest.mark.parametrize(
    "body",
    [
        # a type change: old code reading the column may no longer parse it
        "op.alter_column('t', 'c', type_=sa.Text())",
        # a NOT NULL column with no default: old code inserting rows now fails
        "op.add_column('t', sa.Column('c', sa.Integer(), nullable=False))",
        # Alembic's usual formatting: nullable=False after a nested call
        "op.alter_column(\n        't', 'c',\n        existing_type=sa.String(length=10),\n        nullable=False,\n    )",
        # SQL with no literal text: nothing to check, so it cannot pass
        "op.execute(build_sql())",
        "op.execute(\"ALTER TABLE t ALTER COLUMN c TYPE bigint\")",
    ],
)
def test_contracting_changes_a_line_match_missed_are_refused(tmp_path, body):
    m = tmp_path / "0008_x.py"
    m.write_text(f"def upgrade():\n    {body}\n")
    assert contracting_operations(m.read_text()), body
    assert guard([m], released={"0.67.4"})


@pytest.mark.parametrize(
    "body",
    [
        "op.add_column('t', sa.Column('c', sa.Integer(), nullable=False, server_default='0'))",
        "op.alter_column('t', 'c', nullable=True)",
        # the schema is interpolated; the SQL is still read by its literal parts
        "op.execute(f'CREATE INDEX ix ON {schema}.t (c)')",
        "op.execute(sa.text('UPDATE t SET c = 0 WHERE c IS NULL'))",
    ],
)
def test_expanding_changes_still_pass(tmp_path, body):
    m = tmp_path / "0009_x.py"
    m.write_text(f"def upgrade():\n    {body}\n")
    assert contracting_operations(m.read_text()) == [], body


def test_an_interpolated_drop_is_still_read(tmp_path):
    m = tmp_path / "0010_x.py"
    m.write_text("def upgrade():\n    op.execute(f'ALTER TABLE {schema}.t DROP COLUMN c')\n")
    assert contracting_operations(m.read_text())


def test_a_migration_that_does_not_parse_is_refused(tmp_path):
    m = tmp_path / "0011_x.py"
    m.write_text("def upgrade(:\n    pass\n")
    assert contracting_operations(m.read_text())


def _git(repo: Path, *args: str) -> None:
    import os
    import subprocess

    env = {
        **{k: v for k, v in os.environ.items() if k in ("PATH", "HOME")},
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.org",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.org",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def test_a_new_extension_is_not_judged_against_a_release_that_lacks_it(tmp_path, monkeypatch):
    """The previous release reads none of a new extension's tables, so its
    own early migrations cannot break it (the false positive on attest)."""
    from axiom.infra.deploy import migration_guard as mg

    old = tmp_path / "src/axiom/extensions/builtins/old/migrations/versions"
    old.mkdir(parents=True)
    (old / "0001.py").write_text("def upgrade():\n    op.create_table('t')\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "r")
    _git(tmp_path, "tag", "v1.0.0")
    new = tmp_path / "src/axiom/extensions/builtins/fresh/migrations/versions"
    new.mkdir(parents=True)
    (new / "0002_rename.py").write_text("def upgrade():\n    op.alter_column('t', 'c', new_column_name='d')\n")
    (old / "0002_drop.py").write_text("def upgrade():\n    op.drop_column('t', 'c')\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "c")
    monkeypatch.chdir(tmp_path)
    names = sorted(p.as_posix() for p in mg._changed_since_last_release())
    assert names == ["src/axiom/extensions/builtins/old/migrations/versions/0002_drop.py"]
