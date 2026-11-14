# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""#1992 on a real PostgreSQL: a principal handle past the old 36/64 limits writes a receipt.

SQLite ignores VARCHAR length, so ``tests/policy/test_action_ledger_handle_length.py`` can only
simulate the server's check. These run against the throwaway database ``real_postgres`` /
``fresh_postgres`` make (they fail rather than skip on CI, which has a Postgres service)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from .conftest import ANN, LOG, SITE, Person, check, open_session, signatory

LONG_NAME = "a-very-long-experimenter-name.example-site-extra"
LONG = Person(LONG_NAME, SITE, ("lead",), "Long Handle")  # handle is 60+ characters
assert len(LONG.handle) > 60


def _lengths(engine, schema):
    cols = {c["name"]: c for c in inspect(engine).get_columns("agent_actions", schema=schema)}
    return cols["agent"]["type"].length, cols["authorised_by"]["type"].length


@pytest.mark.parametrize("old", [(64, 36), (36, 64)])
def test_an_old_narrow_table_is_widened_and_then_takes_a_long_handle(fresh_postgres, old):
    from axiom.infra.db import ensure_schema
    from axiom.policy.action_ledger import (
        HANDLE_LEN,
        LEDGER_SCHEMA,
        LEDGER_TABLE,
        ActionLedger,
    )

    engine = create_engine(fresh_postgres)
    schema = ensure_schema(engine, LEDGER_SCHEMA)
    # Make the table, then narrow it to the shape an older release created.
    ActionLedger(state_dir=".", engine=engine)
    table = f'"{schema}"."{LEDGER_TABLE}"'
    with engine.begin() as c:
        # nosemgrep: avoid-sqlalchemy-text -- schema and table are module constants, not input
        c.execute(text(f"ALTER TABLE {table} ALTER COLUMN agent TYPE varchar({old[0]})"))
        # nosemgrep: avoid-sqlalchemy-text -- schema and table are module constants, not input
        c.execute(text(f"ALTER TABLE {table} ALTER COLUMN authorised_by TYPE varchar({old[1]})"))
    assert _lengths(engine, schema) == old
    with pytest.raises(Exception, match="too long"):  # the failure #1992 reported, on a real server
        with engine.begin() as c:
            c.execute(
                text(
                    f"INSERT INTO {table} (id, ts, agent, op_class, name, candidate, outcome, dry_run, authorised_by) "  # nosemgrep: avoid-sqlalchemy-text
                    "VALUES ('x', now(), :a, 'o', 'n', 'c', 'proceeded', false, :a)"
                ),
                {"a": LONG.handle},
            )

    led = ActionLedger(state_dir=".", engine=engine)  # the code path that widens
    assert _lengths(engine, schema) == (HANDLE_LEN, HANDLE_LEN)
    led.record_action(agent=LONG.handle, op_class="attest.sealed", name="sealed", candidate="x",
                      guards=[], outcome="proceeded", authorised_by=LONG.handle)  # fmt: skip
    got = led.query(op_class="attest.sealed")
    assert got[0]["agent"] == LONG.handle and got[0]["authorised_by"] == LONG.handle
    ActionLedger(state_dir=".", engine=engine)  # idempotent: a second pass leaves it as it is
    assert _lengths(engine, schema) == (HANDLE_LEN, HANDLE_LEN)


def test_a_fresh_table_is_created_wide(fresh_postgres):
    from axiom.infra.db import ensure_schema
    from axiom.policy.action_ledger import HANDLE_LEN, LEDGER_SCHEMA, ActionLedger

    engine = create_engine(fresh_postgres)
    schema = ensure_schema(engine, LEDGER_SCHEMA)
    ActionLedger(state_dir=".", engine=engine)
    assert _lengths(engine, schema) == (HANDLE_LEN, HANDLE_LEN)


def test_a_seal_and_a_package_by_a_long_handle_leave_receipts(world, tmp_path):
    from axiom.extensions.builtins.attest import exports, grants, receipts, roles, service, signing

    from .conftest import auth, keys

    roles.grant(LONG.handle, SITE, "lead", by="@admin:example-site", state_dir=tmp_path)
    open_session(world)
    check(world)
    d = service.create_seal_draft(site_id=SITE, logbook=LOG, summary="Sealed by a long handle.",
                                  origin="human", for_principal=LONG.handle, boundary="session")  # fmt: skip
    pres = service.present(d, modality="screen")
    service.respond(pres.presentation_id, principal=signatory(LONG).principal, answer="sign",
                    via="screen")  # fmt: skip
    token = grants.mint(pres.presentation_id, auth=auth(LONG),
                        device=grants.Device("desk-1", "personal"), signer=signing.signer())  # fmt: skip
    result = service.sign(pres.presentation_id, signatory=signatory(LONG), signer=signing.signer(),
                          source="screen", grant=token, grant_keys=keys())  # fmt: skip
    assert result.anchor is not None
    assert result.receipt_error is None, result.receipt_error

    exports.set_storage(None, base_dir=tmp_path / "storage")
    try:
        row, _ = exports.create(SITE, LOG, exports.parse_filter({}), formats=("jsonl",),
                                requested_by=LONG.handle)  # fmt: skip
    finally:
        exports.set_storage(None)
    assert row["receipt_error"] is None, row["receipt_error"]

    listed = receipts.listed(limit=10)
    mine = {r["what"] for r in listed if r["by"] == LONG.handle}
    assert mine == {"sealed", "packaged"}
    assert ANN.handle not in {r["by"] for r in listed if r["what"] == "sealed"}
