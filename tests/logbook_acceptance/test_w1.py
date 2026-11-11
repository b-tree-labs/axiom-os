# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit W1: the Log page hosts every log, with a launch-pad empty state.

Ticket: docs/working/logbook-units/W1.md in the product repo that holds the plan. The behaviour is
in axiom-appkit (`frontend/src/logbookAcceptance/w1.test.tsx`). These rows pin the part of it the
page reads from Axiom: `GET /api/v1/attest/logbooks` lists every log the node holds, and carries
what the page's header is built from (LB-W1-A8): the display name, each entry type's signing
roles, and the `retention` the declaration states, or `null` when it states none. The page then
says "retention: not declared"; Axiom never supplies a default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import registry  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import load_logbook  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_LOG = (
    ROOT / "docs" / "guides" / "examples" / "example_logbook" / "logbook" / "example_log.toml"
)

KEPT_LOG = """
[logbook]
id = "kept_log"
version = "1"
display = "Kept Log"
retention = { live_days = 730, archive = "site" }
assurance = { posture = "sso" }

[[type]]
id = "NOTE"
meanings = ["authored"]
roles = ["member"]
fields = [{ id = "text", type = "text", label = "Text", required = true }]
"""


@pytest.fixture
def three_logs(client, tmp_path):
    """The demo log the attest fixture registers, the F2 example, and one that declares retention."""
    registry.register_file(EXAMPLE_LOG)
    kept = tmp_path / "kept_log.toml"
    kept.write_text(KEPT_LOG)
    registry.register_file(kept)
    return as_(client, OP1)


def test_lb_w1_a1_the_list_holds_every_log_the_node_has(three_logs):
    body = three_logs.get("/api/v1/attest/logbooks").json()["logbooks"]
    assert {b["id"]: b["display"] for b in body} == {
        "demo_log": registry.get("demo_log").display,
        "example_log": "Example Log",
        "kept_log": "Kept Log",
    }


def test_lb_w1_a8_a_log_carries_its_declared_retention(three_logs):
    body = {b["id"]: b for b in three_logs.get("/api/v1/attest/logbooks").json()["logbooks"]}
    assert body["kept_log"]["retention"] == {"live_days": 730, "archive": "site"}
    one = three_logs.get("/api/v1/attest/logbooks/kept_log").json()
    assert one["retention"] == {"live_days": 730, "archive": "site"}


def test_lb_w1_a8_no_retention_is_null_never_a_default(three_logs):
    body = {b["id"]: b for b in three_logs.get("/api/v1/attest/logbooks").json()["logbooks"]}
    assert "retention" in body["example_log"]
    assert body["example_log"]["retention"] is None


def test_lb_w1_a8_the_header_reads_signing_roles_from_each_type(three_logs):
    body = {b["id"]: b for b in three_logs.get("/api/v1/attest/logbooks").json()["logbooks"]}
    roles = {r for t in body["example_log"]["types"] for r in t["roles"]}
    assert roles == {"member", "lead"}


def test_lb_w1_a8_retention_is_still_reported_as_not_yet_enforced(tmp_path):
    """Carrying the declared value to a surface does not make Axiom enforce it."""
    kept = tmp_path / "kept_log.toml"
    kept.write_text(KEPT_LOG)
    book = load_logbook(kept)
    assert book.retention == {"live_days": 730, "archive": "site"}
    assert "logbook.retention" in book.not_yet_enforced
