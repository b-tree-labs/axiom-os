# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for #2243: the follow-ups to time-boxed reviewer access (#1817).

- The blank form, the import dry run and the export list are doors of the one check (LD-1), so
  the generated matrix covers them. A person whose only standing at the site is a reviewer grant
  reaches no other log through them; the change narrows and never widens.
- ``axi gate issue api-key --until`` issues a key that ends by itself, recording its expiry and
  its issuer; ``axi grant list`` shows both.
- Review coverage does not count a review that was later retracted, and reads a corrected
  review's range from its correction (the supplement model, design note L2 section 6).

Rows LG-2243-A1 to A7 are in the issue. No domain words: an "audited log" and a "reviews log".
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import registry, roles, service  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import person  # noqa: E402
from axiom.governance import doors as g  # noqa: E402
from axiom.governance import grants as G  # noqa: E402
from axiom.webauth.users import User  # noqa: E402

SITE = "site-a"
LOG = "audited_log"
OPEN_LOG = "open_log"
REVIEWS = "reviews_log"
OP = "@op1:site-a"
REV = User(user_id="rev1", email="rev1@example.org", name="Rev One", roles=("user",), site=SITE,
           attributes={"idp": "entra"})  # fmt: skip
NOBODY = User(user_id="rev2", email="rev2@example.org", name="Rev Two", roles=("user",),
              site=SITE, attributes={"idp": "entra"})  # fmt: skip
REV_H = "@rev1:site-a"
EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "docs/guides/examples/reviews_log/logbook/reviews_log.toml"
)
OWNER = G.Caller(handle="@owner:site-a", surface="cli", owner=True)

#: The three routes #2242 left on the site-role rule, now doors of the one check.
ALIGNED = {
    "attest.http.form_blank": ("GET", "/api/v1/attest/{logbook}/forms/{form}/blank"),
    "attest.http.import_dry_run": ("POST", "/api/v1/attest/{logbook}/imports/dry-run"),
    "attest.http.export_list": ("GET", "/api/v1/attest/{logbook}/exports"),
}


def _log(log_id: str, readers: tuple[str, ...] = ("operator",)) -> None:
    registry.register(parse_logbook({
        "logbook": {"id": log_id, "version": "1", "display": log_id.replace("_", " ").title(),
                    "readers": list(readers)},
        "type": [{"id": "NOTE", "meanings": ["authored"], "roles": ["operator"],
                  "confirm": {"modalities_any": ["screen", "cli"]},
                  "fields": [{"id": "text", "type": "text"}]}],
    }, source=f"test:{log_id}"))  # fmt: skip


@pytest.fixture
def world(client, attest_db, node_signer, tmp_path, monkeypatch):
    """A gated log, a log any sign-in may read and the example reviews log at site-a; op1 is an
    operator, a member and a lead. The reviews log also lets a lead sign on a review (to correct or
    retract it): the example admits only the reviewer, who signs nothing but a review."""
    from axiom.extensions.builtins.attest import access_requests

    monkeypatch.setattr(access_requests, "knowable", lambda *_a, **_k: False)
    monkeypatch.setenv(roles.ROLES_DIR_ENV, str(tmp_path / "attest"))
    monkeypatch.setenv("AXIOM_SITE", SITE)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    (tmp_path / "attest" / "roles.toml").write_text(
        '[[assignment]]\nprincipal = "@op1:site-a"\nsite = "site-a"\n'
        'roles = ["operator", "member", "lead"]\n'
    )
    _log(LOG)
    _log(OPEN_LOG, readers=("member",))
    book = parse_logbook(tomllib.loads(EXAMPLE.read_text()), source="example:reviews_log")
    t = book.type("REVIEW")
    registry.register(replace(book, types=(replace(t, roles=("reviewer", "lead")),)))
    recs = [note(LOG, f"entry {i}", node_signer) for i in range(3)]
    yield {"client": client, "state": tmp_path, "recs": recs, "signer": node_signer}
    for b in (LOG, OPEN_LOG, REVIEWS):
        registry._logbooks.pop(b, None)


def note(log_id: str, text: str, signer) -> dict:
    d = service.create_draft(site_id=SITE, logbook=log_id, entry_type="NOTE", meaning="authored",
                             content={"title": "Note", "fields": {"text": text}}, origin="human",
                             for_principal=OP)  # fmt: skip
    p = service.present(d)
    who = person(roles=("operator",))
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="cli")
    return service.sign(p.presentation_id, signatory=who, signer=signer).record


def _signed(draft_id: str, signer, who) -> dict:
    p = service.present(draft_id)
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="cli")
    return service.sign(p.presentation_id, signatory=who, signer=signer).record


def review(log_id: str, signer, *, frm: int, to: int) -> dict:
    fields = {"logbook": log_id, "finding": "looked at it", "from_seq": str(frm), "to_seq": str(to)}
    d = service.create_draft(site_id=SITE, logbook=REVIEWS, entry_type="REVIEW", meaning="reviewed",
                             content={"title": "Review", "fields": fields}, origin="human",
                             for_principal=REV_H)  # fmt: skip
    return _signed(d, signer, person(posture="attested", roles=(), handle=REV_H))


def lead():
    return person(posture="attested", roles=("lead",), handle=OP)


def correct(target: dict, signer, **fields) -> dict:
    d = service.create_supplement_draft(site_id=SITE, target=target["attestation_id"],
                                        reason="correction", content={"fields": fields},
                                        origin="human", for_principal=OP)  # fmt: skip
    return _signed(d, signer, lead())


def retract(target: dict, signer) -> dict:
    """A retraction (spec-attestation's record model: kind ``retraction``, meaning ``retracted``,
    ``target`` the record it withdraws)."""
    d = service.create_draft(site_id=SITE, logbook=REVIEWS, entry_type="REVIEW", meaning="retracted",
                             content={"title": "Retracted", "body": "reviewed the wrong range",
                                      "fields": {}},
                             origin="human", for_principal=OP, kind="retraction",
                             target=target["attestation_id"])  # fmt: skip
    return _signed(d, signer, lead())


def grant(state: Path, *, logbook: str = LOG) -> None:
    until = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    roles.grant(REV_H, SITE, "reviewer", by="@owner:site-a", state_dir=state, logbook=logbook,
                until=until)  # fmt: skip


def expire(state: Path) -> None:
    rows = roles._load_log_roles(state)
    for r in rows:
        r["until"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    roles._save(state, roles._load(state), rows)


def call(c, user, method: str, path: str, body: dict | None = None):
    c.cookies.clear()
    as_(c, user)
    url = f"/api/v1/attest/{path}{'&' if '?' in path else '?'}site={SITE}"
    r = c.post(url, json=body or {}) if method == "POST" else c.get(url)
    is_json = r.headers.get("content-type", "").startswith("application/json")
    return r.status_code, r.json() if is_json else r.text


def at_the_three(log_id: str) -> dict[str, tuple[str, str, dict | None]]:
    return {
        "attest.http.form_blank": ("GET", f"{log_id}/forms/daily/blank", None),
        "attest.http.import_dry_run": ("POST", f"{log_id}/imports/dry-run", {"content": ""}),
        "attest.http.export_list": ("GET", f"{log_id}/exports", None),
    }


def coverage(c) -> dict:
    status, body = call(c, OP1, "GET", f"{LOG}/reviews/coverage")
    assert status == 200, body
    return body


# -- A1 ------------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-2243-A1")
def test_lg2243_a1_the_three_routes_are_doors_of_the_one_check_and_rows_of_the_matrix():
    from tests.logbook_acceptance.test_ld1 import ROWS

    rows = {p.id for p in ROWS}
    for door_id, route in ALIGNED.items():
        door = g.get(door_id)
        assert door.route == route and door.shape == "target", door_id
        assert all(f"{door_id}-{g.HTTP}-{r.value}" in rows for r in g.Reason), door_id
        assert route not in g._not_doors, f"{door_id} is still declared not a door"


# -- A2 ------------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-2243-A2")
def test_lg2243_a2_a_reviewer_only_reaches_no_other_log_through_the_three(world):
    """Another log is closed to a person whose only standing is a reviewer grant: the three answer
    exactly as the records door does for a log the person may not read."""
    c = world["client"]
    grant(world["state"])
    refused = call(c, REV, "GET", f"{OPEN_LOG}/records")
    assert refused[0] == 404, refused
    for door_id, (method, path, body) in at_the_three(OPEN_LOG).items():
        assert call(c, REV, method, path, body) == refused, door_id
    # The site's own reader is unchanged.
    assert call(c, OP1, "GET", f"{OPEN_LOG}/exports")[0] == 200


@pytest.mark.acceptance("LG-2243-A2")
def test_lg2243_a2_at_the_granted_log_the_three_narrow_and_never_widen(world):
    """Inside the window the reviewer reads the granted log's export list (a door since #1817). The
    blank form and the import dry run keep the site-role rule as well as the one check, so this
    change opens neither to a reviewer (narrowing only; whether it should is the owner's call).
    After the expiry all three answer exactly as for a person who never held a grant."""
    c, state = world["client"], world["state"]
    grant(state)
    assert call(c, REV, "GET", f"{LOG}/exports")[0] == 200
    for door_id in ("attest.http.form_blank", "attest.http.import_dry_run"):
        method, path, req = at_the_three(LOG)[door_id]
        assert call(c, REV, method, path, req) == call(c, NOBODY, method, path, req), door_id
    expire(state)
    for door_id, (method, path, req) in at_the_three(LOG).items():
        assert call(c, REV, method, path, req) == call(c, NOBODY, method, path, req), door_id


@pytest.mark.acceptance("LG-2243-A2")
def test_lg2243_a2_an_undeclared_log_is_refused_at_the_three(world):
    """The old helper answered an undeclared log as readable; the one check refuses it unlabeled."""
    c = world["client"]
    for door_id, (method, path, body) in at_the_three("no_such_log").items():
        status, got = call(c, OP1, method, path, body)
        assert status == 404 and set(got) == {"detail", "code"}, (door_id, got)


@pytest.mark.acceptance("LG-2243-A2")
def test_lg2243_a2_each_answer_at_the_three_writes_one_read_record(world):
    from axiom.infra import audit_trail

    c, state = world["client"], world["state"]
    grant(state)
    chain = audit_trail.audit_path(state)
    for door_id, (method, path, body) in at_the_three(OPEN_LOG).items():
        before = len(audit_trail.read_chain(chain)) if chain.exists() else 0
        call(c, REV, method, path, body)
        mine = [r for r in audit_trail.read_chain(chain)[before:]
                if (r.get("access") or {}).get("door") == door_id]  # fmt: skip
        assert len(mine) == 1 and mine[0]["outcome"] == "denied", (door_id, mine)


# -- A3 ------------------------------------------------------------------------------------------


def gate(argv: list[str], keys: Path, capsys) -> tuple[int, str]:
    from axiom.extensions.builtins.webgate import cli

    code = cli.main(["--keys-file", str(keys), *argv])
    out = capsys.readouterr()
    return code, out.out + out.err


@pytest.mark.acceptance("LG-2243-A3")
def test_lg2243_a3_gate_issue_until_records_the_expiry_and_the_issuer(tmp_path, capsys):
    keys = tmp_path / "keys.json"
    code, out = gate(["issue", "api-key", "--principal", "@svc:site-a", "--scope", "attest:read",
                      "--until", "2099-01-01"], keys, capsys)  # fmt: skip
    assert code == 0, out
    rec = json.loads(keys.read_text())[-1]
    assert rec["expires_at"] == "2099-01-01T23:59:59+00:00"
    assert rec["issuer"].startswith("@"), "the terminal's principal issued it"
    assert "2099-01-01" in out and rec["issuer"] in out, (
        "the terminal says when it ends and who issued it"
    )
    # An ISO time is taken as given.
    code, out = gate(["issue", "api-key", "--principal", "@svc2:site-a", "--scope", "attest:read",
                      "--until", "2099-02-01T12:00:00Z"], keys, capsys)  # fmt: skip
    assert code == 0, out
    assert json.loads(keys.read_text())[-1]["expires_at"] == "2099-02-01T12:00:00+00:00"


@pytest.mark.acceptance("LG-2243-A3")
def test_lg2243_a3_gate_issue_refuses_an_end_in_the_past_or_not_a_time(tmp_path, capsys):
    keys = tmp_path / "keys.json"
    for until, words in (("2000-01-01", "past"), ("next tuesday", "not a date")):
        code, out = gate(["issue", "api-key", "--principal", "@svc:site-a", "--scope", "attest:read",
                          "--until", until], keys, capsys)  # fmt: skip
        assert code != 0 and words in out, (until, out)
    assert not keys.exists() or json.loads(keys.read_text()) == [], "nothing was issued"


@pytest.mark.acceptance("LG-2243-A3")
def test_lg2243_a3_a_key_issued_without_until_is_unchanged(tmp_path, capsys):
    keys = tmp_path / "keys.json"
    code, out = gate(["issue", "api-key", "--principal", "@svc:site-a", "--scope", "attest:read"],
                     keys, capsys)  # fmt: skip
    assert code == 0, out
    assert "expires_at" not in json.loads(keys.read_text())[-1]


# -- A4 ------------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-2243-A4")
def test_lg2243_a4_grant_list_shows_the_keys_end_and_issuer_and_after_it_the_key_is_no_key(
    tmp_path, monkeypatch, capsys
):
    import axiom.webauth.api_keys as ak
    from axiom.extensions.builtins.webgate.grant_source import ApiKeyGrants

    keys = tmp_path / "keys.json"
    soon = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
    code, out = gate(["--json", "issue", "api-key", "--principal", "@svc:site-a", "--scope",
                      "attest:read", "--until", soon.isoformat()], keys, capsys)  # fmt: skip
    assert code == 0, out
    value = json.loads(out)["value"]
    token = value["token"]
    assert value["issuer"].startswith("@")
    rows = G.list_grants(caller=OWNER, kind="read", sources=[ApiKeyGrants(keys)])
    assert [(r.expires_at, r.issuer) for r in rows] == [(soon, value["issuer"])]
    store = ak.JsonFileApiKeyStore(keys)
    assert store.resolve(token) is not None
    monkeypatch.setattr(ak, "_now", lambda: soon + timedelta(seconds=1))
    assert store.resolve(token) is None, "after its end the key resolves as no key does"


@pytest.mark.acceptance("LG-2243-A4")
def test_lg2243_a4_the_reviewer_grant_from_the_terminal_names_its_issuer_and_end(world):
    from axiom.extensions.builtins.attest.grant_source import ReviewerGrants
    from axiom.extensions.builtins.attest.skills import bind_default
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skill_dispatch import invoke_capability
    from axiom.infra.skills import SkillContext

    state = world["state"]
    ctx = SkillContext(registry=bind_default(), state_dir=state, logger=None,
                       principal=PrincipalContext(handle="@owner:site-a", posture="sso", assured=True),
                       actor="@owner:site-a", surface="cli")  # fmt: skip
    res = invoke_capability(ctx.registry, "attest.role_grant",
                            {"principal": REV_H, "role": "reviewer", "site": SITE, "logbook": LOG,
                             "until": "2099-01-01"}, ctx, surface="cli")  # fmt: skip
    assert res.ok, res.errors
    assert res.value["by"] == "@owner:site-a" and "@owner:site-a" in res.actions_taken[0]
    [row] = G.list_grants(caller=OWNER, kind="read", sources=[ReviewerGrants(state)])
    assert (row.issuer, row.expires_at.date().isoformat()) == ("@owner:site-a", "2099-01-01")


# -- A5 to A7 ------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-2243-A5")
def test_lg2243_a5_a_retracted_review_does_not_count(world):
    c, signer = world["client"], world["signer"]
    grant(world["state"])
    kept = review(LOG, signer, frm=1, to=1)
    wrong = review(LOG, signer, frm=2, to=3)
    assert coverage(c)["reviewed"] == 3
    retract(wrong, signer)
    body = coverage(c)
    assert (body["reviews"], body["reviewed"], body["never_reviewed"]) == (1, 1, [[2, 3]])
    assert body["last_review"]["attestation_id"] == kept["attestation_id"]
    assert body["retracted"] == 1


@pytest.mark.acceptance("LG-2243-A6")
def test_lg2243_a6_a_corrected_review_counts_with_its_corrected_range(world):
    c, signer = world["client"], world["signer"]
    grant(world["state"])
    first = review(LOG, signer, frm=1, to=3)
    correct(first, signer, to_seq="1")
    body = coverage(c)
    assert (body["reviews"], body["reviewed"], body["never_reviewed"]) == (1, 1, [[2, 3]])
    # A correction of a correction: the last one to state a field wins (design note L2, 6).
    second = review(LOG, signer, frm=2, to=2)
    fix = correct(second, signer, from_seq="3", to_seq="3")
    correct(fix, signer, from_seq="2", to_seq="3")
    assert coverage(c)["never_reviewed"] == []


@pytest.mark.acceptance("LG-2243-A7")
def test_lg2243_a7_a_review_corrected_to_name_another_log_leaves_this_ones_coverage(world):
    c, signer = world["client"], world["signer"]
    grant(world["state"])
    first = review(LOG, signer, frm=1, to=3)
    correct(first, signer, logbook=OPEN_LOG)
    body = coverage(c)
    assert (body["reviews"], body["reviewed"], body["last_review"]) == (0, 0, None)
