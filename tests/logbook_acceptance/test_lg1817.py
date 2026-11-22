# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for #1817: time-boxed reviewer access, signed reviews and review coverage.

A reviewer holds a read grant in ADR-205's shape, ``(principal, role, where, expires_at)`` plus
its issuer: the log role ``reviewer``, held for one log at one site until a stated time. Inside
the window the reviewer reads that log through the one check (LD-1) and signs reviews in a reviews
log (meaning ``reviewed``, ADR-274); after it, or after a revoke, every door answers exactly as it
does for a principal who never held a grant. The site reads review coverage of a log in one query.
Rows LG-1817-A1 to A10 are in the issue. No domain words: an "audited log" and a "reviews log".
"""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import registry, roles, service  # noqa: E402
from axiom.extensions.builtins.attest.grant_source import ReviewerGrants  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import MEANINGS, parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.service import AttestRefused  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import person  # noqa: E402
from axiom.governance import doors as g  # noqa: E402
from axiom.governance import grants as G  # noqa: E402
from axiom.webauth.users import User  # noqa: E402

SITE = "site-a"
LOG = "audited_log"
OTHER_LOG = "other_log"
REVIEWS = "reviews_log"
OP = "@op1:site-a"
REV = User(user_id="rev1", email="rev1@example.org", name="Rev One", roles=("user",), site=SITE,
           attributes={"idp": "entra"})  # fmt: skip
NOBODY = User(user_id="rev2", email="rev2@example.org", name="Rev Two", roles=("user",),
              site=SITE, attributes={"idp": "entra"})  # fmt: skip
REV_H = "@rev1:site-a"
EXAMPLE = Path(__file__).resolve().parents[2] / "docs/guides/examples/reviews_log/logbook/reviews_log.toml"
OWNER = G.Caller(handle="@owner:site-a", surface="cli", owner=True)


def _log(log_id: str) -> None:
    registry.register(parse_logbook({
        "logbook": {"id": log_id, "version": "1", "display": log_id.replace("_", " ").title(),
                    "readers": ["operator"]},
        "type": [{"id": "NOTE", "meanings": ["authored"], "roles": ["operator"],
                  "confirm": {"modalities_any": ["screen", "cli"]},
                  "fields": [{"id": "text", "type": "text"}]}],
    }, source=f"test:{log_id}"))  # fmt: skip


@pytest.fixture
def world(client, attest_db, node_signer, tmp_path, monkeypatch):
    """Two gated logs and the example reviews log at site-a; op1 is an operator and a member."""
    from axiom.extensions.builtins.attest import access_requests

    # Nobody here can already know of a log they may not read: the refusal is the 404 that names
    # nothing (the knowable 403 is test_access_request's, as in test_ld1).
    monkeypatch.setattr(access_requests, "knowable", lambda *_a, **_k: False)
    monkeypatch.setenv(roles.ROLES_DIR_ENV, str(tmp_path / "attest"))
    monkeypatch.setenv("AXIOM_SITE", SITE)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    (tmp_path / "attest" / "roles.toml").write_text(
        '[[assignment]]\nprincipal = "@op1:site-a"\nsite = "site-a"\nroles = ["operator", "member"]\n'
    )
    _log(LOG)
    _log(OTHER_LOG)
    registry.register(parse_logbook(tomllib.loads(EXAMPLE.read_text()), source="example:reviews_log"))
    recs = [note(LOG, f"entry {i}", node_signer) for i in range(3)]
    note(OTHER_LOG, "elsewhere", node_signer)
    yield {"client": client, "state": tmp_path, "recs": recs, "signer": node_signer}
    for b in (LOG, OTHER_LOG, REVIEWS):
        registry._logbooks.pop(b, None)


def note(log_id: str, text: str, signer) -> dict:
    d = service.create_draft(site_id=SITE, logbook=log_id, entry_type="NOTE", meaning="authored",
                             content={"title": "Note", "fields": {"text": text}}, origin="human",
                             for_principal=OP)  # fmt: skip
    p = service.present(d)
    who = person(roles=("operator",))
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="cli")
    return service.sign(p.presentation_id, signatory=who, signer=signer).record


def review(log_id: str, signer, *, frm: int | None = None, to: int | None = None,
           handle: str = REV_H) -> dict:  # fmt: skip
    fields = {"logbook": log_id, "finding": "looked at it"}
    if frm is not None:
        fields.update(from_seq=str(frm), to_seq=str(to))
    d = service.create_draft(site_id=SITE, logbook=REVIEWS, entry_type="REVIEW", meaning="reviewed",
                             content={"title": "Review", "fields": fields}, origin="human",
                             for_principal=handle)  # fmt: skip
    p = service.present(d)
    who = person(posture="attested", roles=(), handle=handle)
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="cli")
    return service.sign(p.presentation_id, signatory=who, signer=signer).record


def grant(state: Path, *, until: str | None = None, logbook: str = LOG) -> None:
    until = until or (datetime.now(UTC) + timedelta(days=7)).isoformat()
    roles.grant(REV_H, SITE, "reviewer", by="@owner:site-a", state_dir=state, logbook=logbook,
                until=until)  # fmt: skip


def expire(state: Path) -> None:
    """Move the reviewer's grant into the past, as time passing would."""
    rows = roles._load_log_roles(state)
    for r in rows:
        r["until"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    roles._save(state, roles._load(state), rows)


def get(c, user, path: str):
    c.cookies.clear()
    as_(c, user)
    r = c.get(f"/api/v1/attest/{path}{'&' if '?' in path else '?'}site={SITE}")
    return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text


def reviewer_grant(state: Path) -> G.Grant:
    return next(x for x in ReviewerGrants(state).list())


# -- A1 ------------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1817-A1")
def test_lg1817_a1_a_reviewer_grant_is_one_log_until_a_time_with_its_issuer(world):
    state = world["state"]
    with pytest.raises(ValueError, match="--log"):
        roles.grant(REV_H, SITE, "reviewer", by="@owner:site-a", state_dir=state, until="2099-01-01")
    with pytest.raises(ValueError, match="--until"):
        roles.grant(REV_H, SITE, "reviewer", by="@owner:site-a", state_dir=state, logbook=LOG)
    with pytest.raises(ValueError, match="past"):
        grant(state, until="2000-01-01")
    grant(state, until="2099-01-01")
    rows = G.list_grants(caller=OWNER, kind="read", sources=[ReviewerGrants(state)])
    assert len(rows) == 1
    got = rows[0]
    assert (got.kind, got.store, got.principal, got.issuer) == ("read", "attest", REV_H, "@owner:site-a")
    assert got.scope["where"] == f"site:{SITE}" and got.scope["logbook"] == LOG
    assert got.scope["role"] == "reviewer"
    assert got.expires_at == datetime(2099, 1, 1, 23, 59, 59, 999999, tzinfo=UTC)
    # The principal sees their own grant; a stranger sees nothing.
    mine = G.Caller(handle=REV_H, surface="mcp")
    assert [x.id for x in G.list_grants(caller=mine, sources=[ReviewerGrants(state)])] == [got.id]
    stranger = G.Caller(handle="@x:site-a", surface="cli")
    assert G.list_grants(caller=stranger, sources=[ReviewerGrants(state)]) == []


# -- A2 ------------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1817-A2")
def test_lg1817_a2_inside_the_window_the_reviewer_reads_the_log_and_only_it(world):
    c, state = world["client"], world["state"]
    assert get(c, REV, f"{LOG}/records")[0] == 404, "no grant, no read"
    grant(state)
    status, body = get(c, REV, f"{LOG}/records")
    assert status == 200, body
    assert {r["attestation_id"] for r in body["records"]} >= {r["attestation_id"] for r in world["recs"]}
    assert get(c, REV, f"{OTHER_LOG}/records")[0] == 404, "another log at the site stays hidden"
    assert get(c, REV, f"{REVIEWS}/records")[0] == 404, "the grant does not open the reviews log"
    # Another site: the session is for site-a, so site-b is refused at the site step.
    c.cookies.clear()
    as_(c, REV)
    assert c.get(f"/api/v1/attest/{LOG}/records?site=site-b").status_code == 403


@pytest.mark.acceptance("LG-1817-A2")
def test_lg1817_a2_the_grant_confers_no_signing_role_in_the_log(world):
    grant(world["state"])
    d = service.create_draft(site_id=SITE, logbook=LOG, entry_type="NOTE", meaning="authored",
                             content={"title": "Note", "fields": {"text": "x"}}, origin="human",
                             for_principal=REV_H)  # fmt: skip
    p = service.present(d)
    who = person(posture="sso", roles=(), handle=REV_H)
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="cli")
    with pytest.raises(AttestRefused, match="holds no role"):
        service.sign(p.presentation_id, signatory=who, signer=world["signer"])


@pytest.mark.acceptance("LG-1817-A2")
def test_lg1817_a2_the_one_check_admits_the_grant_through_skill_doors_too(world):
    from axiom.extensions.builtins.attest import doors

    grant(world["state"])
    caller = doors.LogCaller(handle=REV_H, sites=frozenset({SITE}), state_dir=world["state"])
    assert doors.readable(caller, "attest.show", SITE, LOG)
    assert not doors.readable(caller, "attest.show", SITE, OTHER_LOG)
    assert not doors.readable(caller, "attest.show", "site-b", LOG)


# -- A3 ------------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1817-A3")
def test_lg1817_a3_an_expired_grant_answers_exactly_like_no_grant(world):
    c, state = world["client"], world["state"]
    grant(state)
    assert get(c, REV, f"{LOG}/records")[0] == 200
    expire(state)
    for path in (f"{LOG}/records", f"{LOG}/records/{world['recs'][0]['attestation_id']}",
                 f"{LOG}/verify", f"{LOG}/reviews/coverage"):  # fmt: skip
        if path.endswith("verify"):
            c.cookies.clear()
            a = as_(c, REV).post(f"/api/v1/attest/{path}?site={SITE}")
            c.cookies.clear()
            b = as_(c, NOBODY).post(f"/api/v1/attest/{path}?site={SITE}")
            assert (a.status_code, a.json()) == (b.status_code, b.json()), path
            continue
        assert get(c, REV, path) == get(c, NOBODY, path), path
    from axiom.extensions.builtins.attest import doors

    caller = doors.LogCaller(handle=REV_H, sites=frozenset({SITE}), state_dir=state)
    nobody = doors.LogCaller(handle="@rev2:site-a", sites=frozenset({SITE}), state_dir=state)
    t = doors.target(SITE, LOG)
    a, b = g.check(g.get("attest.show"), caller, t), g.check(g.get("attest.show"), nobody, t)
    assert (a.allowed, a.reason, a.status) == (b.allowed, b.reason, b.status) == (False, g.Reason.NOT_PERMITTED, 404)


# -- A4 ------------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1817-A4")
def test_lg1817_a4_a_revoke_at_the_cli_ends_the_grant_on_the_next_read(world):
    c, state = world["client"], world["state"]
    grant(state)
    assert get(c, REV, f"{LOG}/records")[0] == 200
    the = reviewer_grant(state)
    src = [ReviewerGrants(state)]
    with pytest.raises(G.GrantRefused) as off_cli:
        G.revoke(the.id, caller=G.Caller(handle=REV_H, surface="mcp"), sources=src)
    assert off_cli.value.code == "cli_only"
    with pytest.raises(G.GrantNotFound):
        G.revoke(the.id, caller=G.Caller(handle="@x:site-a", surface="cli"), sources=src)
    out = G.revoke(the.id, caller=G.Caller(handle="@owner:site-a", surface="cli"), reason="done",
                   sources=src)  # fmt: skip
    assert out["kind"] == "read" and out["principal"] == REV_H
    assert get(c, REV, f"{LOG}/records") == get(c, NOBODY, f"{LOG}/records")
    assert G.list_grants(caller=OWNER, sources=src) == []
    changes = roles.changes_file(state).read_text().splitlines()
    assert '"revoke"' in changes[-1] and '"@owner:site-a"' in changes[-1]


# -- A5 ------------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1817-A5")
def test_lg1817_a5_keeps_sweep_reads_the_reviewer_grant(world, monkeypatch):
    from axiom.extensions.builtins.vault.skills import grants as grant_skills

    state = world["state"]
    soon = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    grant(state, until=soon)
    monkeypatch.setattr(G, "default_sources", lambda params=None: [ReviewerGrants(state)])
    got = grant_skills.expiry({}, None).value
    assert got["by_kind"]["read"] is not None
    assert [d["principal"] for d in got["due"]] == [REV_H]
    expire(state)
    got = grant_skills.expiry({}, None).value
    assert got["count"] == 0 and got["by_kind"]["read"] is None


@pytest.mark.acceptance("LG-1817-A5")
def test_lg1817_a5_the_reviewer_grant_is_one_of_keeps_default_sources():
    assert any(type(s).__name__ == "ReviewerGrants" for s in G.default_sources())


# -- A6 ------------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1817-A6")
def test_lg1817_a6_reviewed_is_a_meaning_and_the_example_reviews_log_loads():
    assert "reviewed" in MEANINGS
    book = parse_logbook(tomllib.loads(EXAMPLE.read_text()), source="example")
    t = book.type("REVIEW")
    assert t.meanings == ("reviewed",) and t.roles == ("reviewer",)
    assert {f.id for f in t.fields} >= {"logbook", "from_seq", "to_seq", "finding"}
    assert book.not_yet_enforced == () or not book.not_yet_enforced
    text = EXAMPLE.read_text().lower()
    adr = Path(__file__).resolve().parents[2] / "docs/adrs"
    assert any(p.name.startswith("adr-274-") for p in adr.iterdir())
    assert "reviewer" in text


# -- A7 ------------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1817-A7")
def test_lg1817_a7_a_reviewer_signs_a_review_only_of_a_log_their_live_grant_covers(world):
    state, signer = world["state"], world["signer"]
    with pytest.raises(AttestRefused, match="reviewer"):
        review(LOG, signer, frm=1, to=2)
    grant(state)
    rec = review(LOG, signer, frm=1, to=2)
    assert rec["meaning"] == "reviewed" and rec["signer"]["principal"] == REV_H
    with pytest.raises(AttestRefused, match="reviewer"):
        review(OTHER_LOG, signer, frm=1, to=1)
    expire(state)
    with pytest.raises(AttestRefused, match="reviewer"):
        review(LOG, signer, frm=3, to=3)


# -- A8 ------------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1817-A8")
def test_lg1817_a8_coverage_in_one_query(world):
    c, state, signer = world["client"], world["state"], world["signer"]
    status, empty = get(c, OP1, f"{LOG}/reviews/coverage")
    assert status == 200, empty
    assert (empty["reviews"], empty["last_review"], empty["records"], empty["reviewed"]) == (0, None, 3, 0)
    assert empty["never_reviewed"] == [[1, 3]]
    grant(state)
    review(LOG, signer, frm=1, to=2)
    last = review(LOG, signer, frm=2, to=2)
    status, body = get(c, OP1, f"{LOG}/reviews/coverage")
    assert status == 200, body
    assert body["reviews"] == 2 and body["records"] == 3 and body["reviewed"] == 2
    assert body["never_reviewed"] == [[3, 3]]
    assert body["last_review"]["attestation_id"] == last["attestation_id"]
    assert body["last_review"]["signer"] == REV_H and body["last_review"]["at"]
    assert body["periods"] and sum(p["records"] for p in body["periods"]) == 3
    assert sum(p["reviewed"] for p in body["periods"]) == 2


@pytest.mark.acceptance("LG-1817-A8")
def test_lg1817_a8_coverage_is_a_registered_door_on_http_and_skills():
    http = g.get("attest.http.reviews_coverage")
    assert http.route == ("GET", "/api/v1/attest/{logbook}/reviews/coverage")
    assert http.shape == "target" and http.serves == "reviews"
    skill = g.get("attest.reviews_coverage")
    assert skill.skill == "attest.reviews_coverage" and set(skill.transports) == set(g.SKILL_TRANSPORTS)
    from axiom.extensions.builtins.attest.skills import DOOR_SKILLS

    assert "attest.reviews_coverage" in DOOR_SKILLS


# -- A9 ------------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1817-A9")
def test_lg1817_a9_coverage_counts_only_reviews_the_caller_may_read(world):
    c, state, signer = world["client"], world["state"], world["signer"]
    grant(state)
    review(LOG, signer, frm=1, to=3)
    # The reviewer may read the audited log, not the reviews log: zero reviews, no hidden count.
    status, body = get(c, REV, f"{LOG}/reviews/coverage")
    assert status == 200
    assert (body["reviews"], body["last_review"], body["reviewed"]) == (0, None, 0)
    assert "hidden" not in str(body).lower()
    # A caller refused the audited log gets the refused log's answer, with no count.
    assert get(c, NOBODY, f"{LOG}/reviews/coverage") == get(c, NOBODY, f"{LOG}/records")
    # The site sees the review.
    assert get(c, OP1, f"{LOG}/reviews/coverage")[1]["reviews"] == 1


@pytest.mark.acceptance("LG-1817-A9")
def test_lg1817_a9_reviews_at_another_site_never_count(world):
    from axiom.extensions.builtins.attest import reviews

    grant(world["state"])
    review(LOG, world["signer"], frm=1, to=3)
    assert reviews.coverage("site-b", LOG, may_read=lambda _log: True)["reviews"] == 0


# -- A10 -----------------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1817-A10")
def test_lg1817_a10_a_gate_key_carries_an_expiry_and_an_issuer(tmp_path):
    from axiom.extensions.builtins.webgate.grant_source import ApiKeyGrants
    from axiom.webauth.api_keys import JsonFileApiKeyStore, append_api_key_record, mint_api_key

    keys = tmp_path / "keys.json"
    plain_token, plain = mint_api_key(principal="@a:site-a", scopes=["attest:read"], site=SITE)
    append_api_key_record(keys, plain)
    soon = datetime.now(UTC) + timedelta(hours=1)
    token, rec = mint_api_key(principal="@b:site-a", scopes=["attest:read"], site=SITE,
                              expires_at=soon, issuer="@owner:site-a")  # fmt: skip
    append_api_key_record(keys, rec)
    by_id = {x.id: x for x in ApiKeyGrants(keys).list()}
    assert by_id[rec["key_id"]].issuer == "@owner:site-a"
    assert by_id[rec["key_id"]].expires_at == soon.replace(microsecond=0)
    assert by_id[plain["key_id"]].expires_at is None and by_id[plain["key_id"]].issuer is None
    store = JsonFileApiKeyStore(keys)
    assert store.resolve(token) is not None and store.resolve(plain_token) is not None
    import axiom.webauth.api_keys as ak

    later = datetime.now(UTC) + timedelta(hours=2)
    orig = ak._now
    ak._now = lambda: later
    try:
        assert store.resolve(token) is None, "an expired key is no key"
        assert store.resolve(plain_token) is not None
    finally:
        ak._now = orig
