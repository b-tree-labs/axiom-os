# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A vault that cannot be written stops `dev up` cleanly, with nothing half made (#1790).

A fresh contributor cancelled the macOS keychain prompt. `dev up` died with a
``RuntimeError: keychain write failed (rc=154)`` traceback, after it had
already written the account: the next run found an account whose password
nobody held.
"""

from __future__ import annotations

from axiom.extensions.builtins.dev import node
from axiom.extensions.builtins.dev.skills import up
from axiom.webauth import load_user_records

from .conftest import FakeVault
from .test_dev_up_names_its_database_and_never_guesses_one import _params

KEYCHAIN_SAID = (
    "keychain write failed for 'foreign/dev-gate-lane1' (rc=154): User canceled the operation."
)


def test_the_password_is_stored_before_the_account_is_made(tmp_path):
    plan = node.build_plan(lane="x", root=tmp_path, port=1, state_dir=tmp_path / "state")
    vault = FakeVault(fail_writes=KEYCHAIN_SAID)
    try:
        node.ensure_owner(plan, "person@example.org", vault)
    except RuntimeError:
        pass
    accounts = load_user_records(plan.accounts) if plan.accounts.exists() else []
    assert accounts == []


def test_dev_up_names_the_fix_instead_of_a_traceback(harness, monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u:p@db.example:6543/scratch")
    harness["vault"].fail_writes = KEYCHAIN_SAID
    result = up.run(_params(), harness["ctx"])
    assert not result.ok
    said = "\n".join(result.errors)
    assert "AXIOM_FOREIGN_SECRETS_BACKEND=file" in said
    assert "rc=154" in said  # what actually went wrong, too
    assert harness["started"] == []
    users = node.home_for(harness["ctx"].state_dir, "lane1") / "users.json"
    assert not users.exists() or load_user_records(users) == []


def test_after_the_fix_the_next_run_makes_the_account(harness, monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u:p@db.example:6543/scratch")
    harness["vault"].fail_writes = KEYCHAIN_SAID
    assert not up.run(_params(), harness["ctx"]).ok
    harness["vault"].fail_writes = ""
    result = up.run(_params(), harness["ctx"])
    assert result.ok, result.errors
    assert result.value["account_created"]
    assert node.secret_name("lane1") in harness["vault"].values
