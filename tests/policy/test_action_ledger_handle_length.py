# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""#1992: a principal handle (``@name:context``) must fit the action ledger's columns.

SQLite does not enforce VARCHAR length, PostgreSQL does, so these assert the declared length and
simulate the server's check on write."""

from __future__ import annotations

from sqlalchemy import create_engine, event

from axiom.policy.action_ledger import HANDLE_LEN, ActionLedger

LONG = (
    "@" + "a-very-long-experimenter-name.example-site" + ":" + "example-site-context-" * 3
)  # 100+ chars


def _ledger(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/l.db")

    @event.listens_for(engine, "before_cursor_execute")
    def _enforce(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        # PostgreSQL's StringDataRightTruncation, for the handle columns
        if statement.lstrip().upper().startswith("INSERT") and isinstance(
            parameters, (tuple, list)
        ):
            table = ActionLedger  # noqa: F841
            for p in parameters:
                if isinstance(p, str) and p.startswith("@") and len(p) > cols_limit[0]:
                    raise RuntimeError(
                        f"value too long for type character varying({cols_limit[0]})"
                    )

    cols_limit = [None]
    led = ActionLedger(state_dir=tmp_path, engine=engine)
    t = led._backend._table
    cols_limit[0] = min(t.c.agent.type.length, t.c.authorised_by.type.length)
    return led


def test_handle_columns_are_wide_enough_for_a_long_handle(tmp_path):
    led = _ledger(tmp_path)
    t = led._backend._table
    assert len(LONG) > 60
    assert t.c.agent.type.length >= len(LONG)
    assert t.c.authorised_by.type.length >= len(LONG)
    assert HANDLE_LEN >= 256


def test_a_long_handle_round_trips(tmp_path):
    led = _ledger(tmp_path)
    led.record_action(agent=LONG, op_class="attest.sealed", name="sealed", candidate="x", guards=[],
                      outcome="proceeded", authorised_by=LONG)  # fmt: skip
    got = led.query(op_class="attest.sealed")
    assert got and got[0]["agent"] == LONG and got[0]["authorised_by"] == LONG


def test_a_receipt_that_cannot_be_written_is_reported_not_swallowed(monkeypatch, tmp_path):
    from axiom.extensions.builtins.attest import receipts
    from axiom.policy import action_ledger

    def boom(self, **kw):
        raise RuntimeError("value too long for type character varying(36)")

    monkeypatch.setattr(action_ledger.ActionLedger, "record_action", boom)
    monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: tmp_path)
    err = receipts._record(op_class="attest.sealed", name="sealed", by="@x", ref="r", metadata={})
    assert err and "sealed receipt was not written" in err and "too long" in err
    seal = {
        "signer": {"principal": "@x"},
        "site_id": "s",
        "logbook": "l",
        "attestation_id": "a",
        "seq": 1,
    }
    assert receipts.sealed(seal, {"anchor_id": "an", "root": "r"}) is not None


def test_widening_leaves_a_wider_column_alone():
    from unittest.mock import MagicMock

    from sqlalchemy import String

    from axiom.policy.action_ledger import _SqlActionBackend

    eng = MagicMock()
    eng.dialect.name = "postgresql"
    cols = {"agent": {"type": String(HANDLE_LEN), "nullable": False},
            "authorised_by": {"type": String(1000), "nullable": True}}  # fmt: skip
    _SqlActionBackend._widen_handles(eng, None, cols)
    eng.begin.assert_not_called()
