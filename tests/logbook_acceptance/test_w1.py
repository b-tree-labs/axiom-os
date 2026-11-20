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


# -- LB-W1-A9: read-only logs, and logs a person may not read -------------------------------

READERS_LOG = """
[logbook]
id = "readers_log"
version = "1"
display = "Readers Log"
readers = ["auditor"]
assurance = { posture = "sso" }

[[type]]
id = "NOTE"
meanings = ["authored"]
roles = ["member"]
fields = [{ id = "text", type = "text", label = "Text", required = true }]
"""


def _grant(state_dir, assignments):
    """Replace the site's role file: ``{principal: [roles]}`` at site-a."""
    (state_dir / "attest").mkdir(exist_ok=True)
    rows = []
    for who, held in assignments.items():
        quoted = ", ".join(f'"{r}"' for r in held)
        rows.append(f'[[assignment]]\nprincipal = "{who}"\nsite = "site-a"\nroles = [{quoted}]\n')
    (state_dir / "attest" / "roles.toml").write_text("".join(rows))


@pytest.fixture
def a9(client, tmp_path):
    log = tmp_path / "readers_log.toml"
    log.write_text(READERS_LOG)
    registry.register_file(log)
    return client, tmp_path


@pytest.mark.acceptance("LB-W1-A9")
def test_lb_w1_a9_a_log_carries_can_sign_for_the_caller_per_log_and_per_type(a9):
    client, state = a9
    _grant(state, {"@op1:site-a": ["operator"]})
    body = {b["id"]: b for b in as_(client, OP1).get("/api/v1/attest/logbooks").json()["logbooks"]}
    demo = body["demo_log"]
    assert demo["can_sign"] is True and demo["read_only"] is False
    by_type = {t["id"]: t["can_sign"] for t in demo["types"]}
    assert by_type == {"ROUND_CHECK": True, "NOTE": True}
    one = client.get("/api/v1/attest/logbooks/demo_log").json()
    assert one["can_sign"] is True and one["read_only"] is False


@pytest.mark.acceptance("LB-W1-A9")
def test_lb_w1_a9_a_log_the_person_may_read_but_not_sign_is_read_only(a9):
    client, state = a9
    _grant(state, {"@op1:site-a": ["auditor"]})
    body = {b["id"]: b for b in as_(client, OP1).get("/api/v1/attest/logbooks").json()["logbooks"]}
    assert body["readers_log"]["read_only"] is True and body["readers_log"]["can_sign"] is False
    assert [t["can_sign"] for t in body["readers_log"]["types"]] == [False]
    # a log with no declared readers is readable by anyone signed in, read-only without a role
    assert body["demo_log"]["read_only"] is True and body["demo_log"]["can_sign"] is False


@pytest.mark.acceptance("LB-W1-A9")
def test_lb_w1_a9_a_log_the_person_may_not_read_is_absent_everywhere(a9):
    client, state = a9
    _grant(state, {"@op1:site-a": ["operator"]})
    as_(client, OP1)
    ids = {b["id"] for b in client.get("/api/v1/attest/logbooks").json()["logbooks"]}
    assert "readers_log" not in ids and "demo_log" in ids
    for path in (
        "/api/v1/attest/logbooks/readers_log",
        "/api/v1/attest/readers_log/records",
        "/api/v1/attest/readers_log/intervals",
        "/api/v1/attest/readers_log/obligations",
        "/api/v1/attest/readers_log/seal/preview",
    ):
        assert client.get(path).status_code == 404, path
    assert client.post("/api/v1/attest/readers_log/verify").status_code == 404
    assert client.post("/api/v1/attest/readers_log/seal", json={"summary": "x"}).status_code == 404
    # a signer may always read the log they sign in
    _grant(state, {"@op1:site-a": ["member"]})
    ids = {b["id"] for b in client.get("/api/v1/attest/logbooks").json()["logbooks"]}
    assert "readers_log" in ids


@pytest.mark.acceptance("LB-W1-A9")
def test_lb_w1_a9_readers_is_validated_and_admin_roles_confer_nothing(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(READERS_LOG.replace('readers = ["auditor"]', 'readers = ["admin"]'))
    with pytest.raises(Exception, match="administration"):
        load_logbook(bad)
    good = tmp_path / "good.toml"
    good.write_text(READERS_LOG)
    book = load_logbook(good)
    assert book.readers == ("auditor",) and "logbook.readers" not in book.not_yet_enforced
