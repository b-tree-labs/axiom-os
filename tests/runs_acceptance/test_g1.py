# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RW-G1: the ``grant`` noun (ADR-250 D4, issue #1740).

One list and one revoke across the four kinds, ``read`` (a gate key, what a key may read),
``act`` (a KEEP capability token), ``sign`` (an ADR-146 signing grant) and ``run`` (a
capability token with a budget and a parent), in one shape. The projection is read-only: each
grant stays in the store that issued it, a revoke is that store's own revoke, and the next use
is refused by that store's own check.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.attest import grants as signing_grants
from axiom.extensions.builtins.attest import service
from axiom.extensions.builtins.attest.grant_source import SigningGrants
from axiom.extensions.builtins.attest.grants import Authentication, Device
from axiom.extensions.builtins.attest.service import AttestRefused
from axiom.extensions.builtins.attest.tests.test_signing import _round_check, person
from axiom.extensions.builtins.vault.capability_store import (
    VaultContext,
    get_capability_by_id,
    issue_capability,
)
from axiom.extensions.builtins.vault.grant_source import CapabilityGrants
from axiom.extensions.builtins.webgate.grant_source import ApiKeyGrants
from axiom.governance import Classification, IntentPattern, ResourcePattern
from axiom.governance import grants as G
from axiom.governance.budget import BudgetEnvelope
from axiom.governance.capability import CapabilityToken
from axiom.governance.intent import ActionIntent
from axiom.governance.resource import ResourceRef
from axiom.infra.principal import PrincipalContext
from axiom.vega.identity.principal import Principal
from axiom.webauth.api_keys import JsonFileApiKeyStore, append_api_key_record, mint_api_key

SHAPE = {"id", "kind", "principal", "scope", "expires_at", "issuer", "parent", "store"}

OWNER = G.Caller(handle="@owner:site-a", surface="cli", owner=True)
STRANGER = G.Caller(handle="@stranger:site-a", surface="cli", owner=False)


def _principal(handle: str) -> Principal:
    return Principal(handle=handle, public_bytes=b"\x01" * 32)


@pytest.fixture
def world(tmp_path, attest_db, node_signer):
    """One grant of each kind, each issued through its own store."""
    keys = tmp_path / "api_keys.json"
    token, record = mint_api_key(principal="@reviewer:site-a", scopes=["attest:read"], site="site-a")
    append_api_key_record(keys, record)

    vault = VaultContext()
    act = issue_capability(
        vault,
        subject=_principal("@agent:site-a"),
        intent_pattern=IntentPattern("notification.send"),
        resource_pattern=ResourcePattern("mailto://ops"),
        classification_ceiling=Classification.INTERNAL,
        delegation_depth=1,
    )
    run = issue_capability(
        vault,
        subject=_principal("@worker:site-a"),
        intent_pattern=IntentPattern("forge.push"),
        resource_pattern=ResourcePattern("git://repo/branch-a"),
        classification_ceiling=Classification.INTERNAL,
        parent_capability=act.id,
        budget=BudgetEnvelope(turns=10, tokens=1000, dollars=1.0, wall_clock_seconds=600),
    )

    pres = service.present(_round_check(), modality="screen")
    service.respond(pres.presentation_id, principal=person().principal, answer="sign", via="screen")
    auth = Authentication(
        principal=PrincipalContext(handle="@op1:site-a", posture="sso", assured=True, idp="entra"),
        auth_time=datetime.now(UTC) - timedelta(minutes=1),
        amr=("pwd",),
    )
    sign_token = signing_grants.mint(
        pres.presentation_id,
        auth=auth,
        device=Device(device_id="desk-7", device_class="personal", console_id="console-1"),
        signer=node_signer,
    )
    sign_claims = signing_grants.read(sign_token, {node_signer.key_id: node_signer.public_bytes})

    sources = [ApiKeyGrants(keys), CapabilityGrants(vault), SigningGrants()]
    return {
        "sources": sources,
        "keys": keys,
        "key_token": token,
        "key_id": record["key_id"],
        "vault": vault,
        "act": act,
        "run": run,
        "pres": pres,
        "sign_token": sign_token,
        "sign_id": sign_claims["grant_id"],
        "signer": node_signer,
    }


def _by_kind(rows):
    return {g.kind: g for g in rows}


# -- RW-G1-A1 -------------------------------------------------------------------------------


def test_rw_g1_a1_every_kind_is_listed_in_one_shape(world):
    """RW-G1-A1: one grant of each kind, issued through its own store, appears in one list."""
    rows = G.list_grants(caller=OWNER, sources=world["sources"])
    kinds = _by_kind(rows)
    assert set(kinds) == {"read", "act", "sign", "run"}
    for g in rows:
        assert set(g.as_dict()) == SHAPE
    assert kinds["read"].id == world["key_id"] and kinds["read"].principal == "@reviewer:site-a"
    assert kinds["read"].scope["where"] == "site:site-a"
    assert kinds["read"].scope["roles"] == ["attest:read"]
    assert kinds["act"].id == world["act"].id and kinds["act"].principal == "@agent:site-a"
    assert kinds["act"].scope["intent"] == "notification.send"
    assert kinds["run"].id == world["run"].id and kinds["run"].parent == world["act"].id
    assert kinds["run"].scope["budget"]["turns"] == 10
    assert kinds["sign"].id == world["sign_id"] and kinds["sign"].principal == "@op1:site-a"
    assert kinds["sign"].scope["digest"] == world["pres"].digest
    assert kinds["sign"].expires_at is not None
    assert {g.store for g in rows} == {"gate-keys", "vault", "attest"}


def test_rw_g1_a1_list_filters_by_principal_and_kind(world):
    """RW-G1-A1: ``--principal`` and ``--kind`` narrow the one list."""
    only = G.list_grants(caller=OWNER, sources=world["sources"], principal="@worker:site-a")
    assert [g.kind for g in only] == ["run"]
    signs = G.list_grants(caller=OWNER, sources=world["sources"], kind="sign")
    assert [g.id for g in signs] == [world["sign_id"]]
    with pytest.raises(ValueError):
        G.list_grants(caller=OWNER, sources=world["sources"], kind="admin")


def test_rw_g1_a1_a_person_sees_only_grants_they_hold_or_issued(world):
    """RW-G1-A1: the list applies the caller's own rights; the owner sees the site's."""
    holder = G.Caller(handle="@agent:site-a", surface="mcp", owner=False)
    assert [g.kind for g in G.list_grants(caller=holder, sources=world["sources"])] == ["act"]
    assert G.list_grants(caller=STRANGER, sources=world["sources"]) == []
    with pytest.raises(G.GrantNotFound):
        G.show(world["act"].id, caller=STRANGER, sources=world["sources"])
    assert G.show(world["act"].id, caller=holder, sources=world["sources"]).kind == "act"


# -- RW-G1-A2 -------------------------------------------------------------------------------


def test_rw_g1_a2_revoke_ends_each_kind_in_its_own_store(world):
    """RW-G1-A2: a revoke ends the grant where it lives; that store's own check refuses it."""
    src = world["sources"]
    for kind in ("read", "act", "run", "sign"):
        grant = _by_kind(G.list_grants(caller=OWNER, sources=src))[kind]
        receipt = G.revoke(grant.id, caller=OWNER, sources=src, reason="test")
        assert receipt["kind"] == kind and receipt["store"] == grant.store
    assert G.list_grants(caller=OWNER, sources=src) == []

    # read: the gate's own key store refuses the key on its next presentation.
    assert JsonFileApiKeyStore(world["keys"]).resolve(world["key_token"]) is None
    # act and run: KEEP's own lookup no longer returns them.
    assert get_capability_by_id(world["vault"], world["act"].id) is None
    assert get_capability_by_id(world["vault"], world["run"].id) is None
    # sign: the signing path refuses the grant.
    with pytest.raises(AttestRefused, match="revoked"):
        service.sign(
            world["pres"].presentation_id,
            signatory=person(),
            signer=world["signer"],
            grant=world["sign_token"],
            grant_keys={world["signer"].key_id: world["signer"].public_bytes},
        )


def test_rw_g1_a2_a_revoke_never_widens(world):
    """RW-G1-A2 (must refuse): revoking one grant leaves every other one exactly as it was."""
    src = world["sources"]
    before = {g.id: g for g in G.list_grants(caller=OWNER, sources=src)}
    G.revoke(world["run"].id, caller=OWNER, sources=src, reason="test")
    after = {g.id: g for g in G.list_grants(caller=OWNER, sources=src)}
    assert set(after) == set(before) - {world["run"].id}
    for gid, g in after.items():
        assert g == before[gid]
    with pytest.raises(G.GrantNotFound):
        G.revoke(world["run"].id, caller=OWNER, sources=src, reason="again")


# -- RW-G1-A3 -------------------------------------------------------------------------------


def test_rw_g1_a3_only_issuer_principal_or_owner_may_revoke(world):
    """RW-G1-A3: a caller who is neither issuer, principal nor owner cannot revoke."""
    src = world["sources"]
    for kind in ("read", "act", "run", "sign"):
        grant = _by_kind(G.list_grants(caller=OWNER, sources=src))[kind]
        # A stranger who somehow knows the id is told the grant does not exist for them.
        with pytest.raises((G.GrantNotFound, G.GrantRefused)):
            G.revoke(grant.id, caller=STRANGER, sources=src, reason="x")
    assert len(G.list_grants(caller=OWNER, sources=src)) == 4
    holder = G.Caller(handle="@agent:site-a", surface="cli", owner=False)
    assert G.revoke(world["act"].id, caller=holder, sources=src, reason="mine")["kind"] == "act"


def test_rw_g1_a3_sign_and_read_are_never_revoked_over_mcp(world):
    """RW-G1-A3: a ``sign`` (and a ``read``) grant cannot be revoked over MCP, even by the owner."""
    src = world["sources"]
    over_mcp = G.Caller(handle="@owner:site-a", surface="mcp", owner=True)
    for gid in (world["sign_id"], world["key_id"]):
        with pytest.raises(G.GrantRefused) as exc:
            G.revoke(gid, caller=over_mcp, sources=src, reason="x")
        assert exc.value.code == "cli_only"
    assert len(G.list_grants(caller=OWNER, sources=src)) == 4


def test_rw_g1_a3_act_and_run_off_the_cli_go_through_guard(world):
    """RW-G1-A3: an ``act`` or ``run`` revoke off the CLI asks GUARD, and a refusal stands."""
    src = world["sources"]
    over_mcp = G.Caller(handle="@owner:site-a", surface="mcp", owner=True)
    asked: list[str] = []

    def refuse(caller, grant):
        asked.append(grant.id)
        return False

    with pytest.raises(G.GrantRefused) as exc:
        G.revoke(world["act"].id, caller=over_mcp, sources=src, reason="x", guard=refuse)
    assert exc.value.code == "guard" and asked == [world["act"].id]
    # With no rule permitting it, the default GUARD check refuses (fail closed).
    with pytest.raises(G.GrantRefused):
        G.revoke(world["run"].id, caller=over_mcp, sources=src, reason="x")
    assert G.revoke(world["act"].id, caller=over_mcp, sources=src, reason="x",
                    guard=lambda c, g: True)["kind"] == "act"  # fmt: skip


def test_rw_g1_a3_the_revoke_skill_is_cli_only_and_the_reads_reach_mcp():
    """RW-G1-A3: ``grant revoke`` is projected on the CLI only; list and show are reads."""
    from axiom.extensions.builtins.vault.skills.grants import SKILLS

    by_name = {s.name: s for s in SKILLS}
    assert by_name["grant.revoke"].surfaces == ("cli",)
    assert by_name["grant.revoke"].risk == "destructive" or by_name["grant.revoke"].side_effects
    for read in ("grant.list", "grant.show"):
        assert "mcp" in by_name[read].surfaces and by_name[read].side_effects is False
        assert by_name[read].risk == "read"


def test_rw_g1_a3_the_revoke_skill_refuses_off_the_cli(world, monkeypatch, tmp_path):
    """RW-G1-A3: the skill itself refuses a sign revoke that did not arrive on the CLI."""
    import logging

    from axiom.extensions.builtins.vault.skills import grants as grant_skills
    from axiom.infra.skills import SkillContext, SkillRegistry

    monkeypatch.setattr(G, "default_sources", lambda params=None: world["sources"])
    ctx = SkillContext(
        registry=SkillRegistry(),
        state_dir=tmp_path,
        logger=logging.getLogger("t"),
        principal=PrincipalContext(handle="@owner:site-a"),
        surface="mcp",
    )
    result = grant_skills.revoke({"id": world["sign_id"]}, ctx)
    assert not result.ok and "CLI" in " ".join(result.errors)
    cli = SkillContext(registry=SkillRegistry(), state_dir=tmp_path,
                       logger=logging.getLogger("t"),
                       principal=PrincipalContext(handle="@owner:site-a"), surface="cli")  # fmt: skip
    assert grant_skills.revoke({"id": world["sign_id"]}, cli).ok
    listed = grant_skills.list_({}, cli)
    assert listed.ok and {g["kind"] for g in listed.value["grants"]} == {"read", "act", "run"}


# -- RW-G1-A4 -------------------------------------------------------------------------------


def test_rw_g1_a4_keeps_expiry_sweep_reads_the_projection(world, monkeypatch, tmp_path):
    """RW-G1-A4: KEEP's sweep reads the one projection and names the earliest expiry."""
    import logging

    from axiom.extensions.builtins.vault.skills import grants as grant_skills
    from axiom.infra.skills import SkillContext, SkillRegistry

    calls: list[dict] = []
    real = G.list_grants

    def spy(**kw):
        calls.append(kw)
        return real(**kw)

    monkeypatch.setattr(G, "default_sources", lambda params=None: world["sources"])
    monkeypatch.setattr(G, "list_grants", spy)
    ctx = SkillContext(registry=SkillRegistry(), state_dir=tmp_path,
                       logger=logging.getLogger("t"), surface="cli")  # fmt: skip
    result = grant_skills.expiry({}, ctx)
    assert calls, "the sweep did not read the projection"
    value = result.value
    assert value["earliest"]["id"] == world["sign_id"] and value["earliest"]["kind"] == "sign"
    assert set(value["by_kind"]) == {"read", "act", "run", "sign"}
    assert value["by_kind"]["read"] is None  # a gate key carries no expiry
    assert value["count"] == 4
    assert {g["id"] for g in value["due"]} >= {world["sign_id"], world["act"].id, world["run"].id}


def test_rw_g1_a4_earliest_expiry_across_kinds(world):
    rows = G.list_grants(caller=OWNER, sources=world["sources"])
    assert G.earliest_expiry(rows).kind == "sign"
    assert G.earliest_expiry([]) is None


# -- must refuse ----------------------------------------------------------------------------


def test_rw_g1_must_refuse_a_grant_minted_outside_keeps_stores(world):
    """Must refuse: a token built outside KEEP's stores is not a grant the noun lists or revokes,
    and a source that is not one of the three stores is refused."""
    stray = CapabilityToken.for_call(
        issuer=_principal("@surface:site-a"),
        subject=_principal("@agent:site-a"),
        intent=ActionIntent("notification.send"),
        resource=ResourceRef(scheme="mailto", identifier="ops"),
        classification=Classification.INTERNAL,
    )
    assert stray.id not in {g.id for g in G.list_grants(caller=OWNER, sources=world["sources"])}
    with pytest.raises(G.GrantNotFound):
        G.revoke(stray.id, caller=OWNER, sources=world["sources"], reason="x")

    class Elsewhere:
        store = "elsewhere"

        def list(self):
            return []

        def revoke(self, grant_id, *, by, reason):
            return {}

    with pytest.raises(G.GrantRefused) as exc:
        G.list_grants(caller=OWNER, sources=[*world["sources"], Elsewhere()])
    assert exc.value.code == "not_keep"


def test_rw_g1_must_refuse_a_fourth_store():
    """Must refuse: the projection mints nothing and holds nothing of its own."""
    assert not hasattr(G, "issue") and not hasattr(G, "mint") and not hasattr(G, "grant")
    text = inspect.getsource(G)
    for forbidden in ("session_for", "declarative_base", "DeclarativeBase", "mapped_column",
                      "write_text", "open("):  # fmt: skip
        assert forbidden not in text, forbidden
    assert set(G.KEEP_STORES) == {"gate-keys", "vault", "attest"}
