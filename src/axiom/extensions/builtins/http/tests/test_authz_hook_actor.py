# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""AuthzHook × ActorContext — ``decide()`` finally receives roles (ADR-084 × ADR-103).

Before this, the HTTP edge resolved a bare ``Principal`` and the directory seam
had no caller on any request path. These tests drive the wiring end to end
with fakes: a credential's verified claims → membership (claims-first, then a
directory, degrading only toward less authority) → ``ActorContext`` +
``SubjectContext`` on the envelope → a role-scoped rule that can now match.
"""

from __future__ import annotations

import hashlib
import warnings
from datetime import timedelta
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.directory.mapping import GroupRoleMap
from axiom.extensions.builtins.directory.resolution import MembershipResolver
from axiom.extensions.builtins.directory.revoked import RevokedSet
from axiom.extensions.builtins.http.actor import (
    actor_resolver_from_env,
    build_actor_resolver,
    slug,
)
from axiom.extensions.builtins.http.authz_hook import (
    ResolvedCredential,
    build_authz_hook,
    build_bearer_resolver,
    build_session_resolver,
    chain_resolvers,
)
from axiom.governance import Decision, Verdict
from axiom.vega.identity.principal import Principal
from axiom.webauth import SESSION_COOKIE, User, create_access_token, issue_session_token
from axiom.webauth.keys import reset_key_store_for_tests

warnings.filterwarnings("ignore")


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _request(
    *,
    path="/v1/chat/completions",
    method="POST",
    mount="gateway",
    headers=None,
    cookies=None,
    base_url="http://node.example/",
):
    return SimpleNamespace(
        state=SimpleNamespace(mount_extension=mount),
        method=method,
        url=SimpleNamespace(path=path),
        headers=headers or {},
        cookies=cookies or {},
        base_url=base_url,
    )


def _principal(handle="@svc:org"):
    return Principal(handle=handle, public_bytes=hashlib.sha256(handle.encode()).digest())


def _cred(handle="@alice:site", claims=None, posture="sso"):
    return ResolvedCredential(principal=_principal(handle), claims=claims or {}, posture=posture)


def _capture():
    seen = {}

    def decide_fn(env):
        seen["env"] = env
        return Verdict.from_decision(Decision.PERMIT, "ok", "rcpt")

    return seen, decide_fn


# ---------------------------------------------------------------- the hook attaches an actor


def test_hook_attaches_actor_context_with_roles_from_claims():
    seen, decide_fn = _capture()
    cred = _cred(
        claims={
            "sub": "oid-1",
            "roles": ["Researcher"],
            "tid": "t-1",
            "email": "a@x.org",
            "idp": "entra",
        }
    )
    hook = build_authz_hook(resolve_principal=lambda r: cred, decide_fn=decide_fn)
    req = _request()
    assert hook(req).allow is True
    env = seen["env"]
    assert env.actor_context is not None
    assert env.actor_context.handle == "@alice:site"
    assert env.actor_context.roles == ("Researcher",)
    assert env.actor_context.tenant == "t-1"
    assert env.actor_context.assurance.posture == "sso"
    assert env.actor_context.assurance.authenticated is True
    assert env.actor_context.attributes["idp"] == "entra"
    # the substrate view rides along too, keyed on the immutable subject
    assert env.subject is not None
    assert env.subject.fga_user == "user:oid-1"
    assert env.subject.tenant == "t-1"
    # and downstream handlers can read it
    assert req.state.actor is env.actor_context


def test_bare_principal_still_gets_an_actor_with_no_roles():
    seen, decide_fn = _capture()
    hook = build_authz_hook(resolve_principal=lambda r: _principal("@svc:org"), decide_fn=decide_fn)
    assert hook(_request()).allow is True
    actor = seen["env"].actor_context
    assert actor.handle == "@svc:org" and actor.roles == ()
    assert actor.assurance.posture == "open"
    assert seen["env"].subject.fga_user == "user:@svc:org"


def test_actor_resolver_failure_denies_nothing_but_grants_nothing():
    seen, decide_fn = _capture()

    class Boom:
        def resolve(self, ref, *, claims=None):
            raise RuntimeError("directory exploded")

    resolver = build_actor_resolver(membership=Boom())
    hook = build_authz_hook(
        resolve_principal=lambda r: _cred(claims={"sub": "oid-1", "roles": ["Admin"]}),
        decide_fn=decide_fn,
        resolve_actor_context=resolver,
    )
    assert hook(_request()).allow is True  # decide() still ran (and permitted)
    actor = seen["env"].actor_context
    assert actor.roles == ()  # the claim's Admin was NOT honoured
    assert actor.attributes["membership_source"] == "error"
    assert seen["env"].subject.contextual_tuples == ()


def test_bad_posture_never_raises_out_of_the_hook():
    seen, decide_fn = _capture()
    hook = build_authz_hook(
        resolve_principal=lambda r: _cred(posture="galactic"), decide_fn=decide_fn
    )
    assert hook(_request()).allow is True
    assert seen["env"].actor_context.roles == ()
    assert seen["env"].actor_context.attributes["resolution"] == "error"


# ---------------------------------------------------------------- the directory seam


def _membership(*, provider=None, rules=(), revoked=None):
    return MembershipResolver(
        provider=provider,
        role_map=GroupRoleMap(rules=tuple(rules)),
        revoked=revoked,
    )


def test_groups_claim_maps_to_roles_and_contextual_tuples():
    seen, decide_fn = _capture()
    m = _membership(rules=[{"group": "grp-operators", "role": "operator"}])
    hook = build_authz_hook(
        resolve_principal=lambda r: _cred(
            claims={"sub": "oid-1", "groups": ["grp-operators", "grp-other"]}
        ),
        decide_fn=decide_fn,
        resolve_actor_context=build_actor_resolver(membership=m),
    )
    assert hook(_request()).allow is True
    env = seen["env"]
    assert env.actor_context.roles == ("operator",)
    assert env.actor_context.attributes["membership_source"] == "claims"
    assert env.subject.fga_user == "user:oid-1"
    assert ("user:oid-1", "member", "group:grp-operators") in env.subject.contextual_tuples
    assert ("user:oid-1", "member", "group:grp-other") in env.subject.contextual_tuples


def test_claims_based_membership_unions_the_token_roles_claim():
    seen, decide_fn = _capture()
    m = _membership(rules=[{"group": "grp-operators", "role": "operator"}])
    hook = build_authz_hook(
        resolve_principal=lambda r: _cred(
            claims={"sub": "oid-1", "groups": ["grp-operators"], "roles": ["Researcher"]}
        ),
        decide_fn=decide_fn,
        resolve_actor_context=build_actor_resolver(membership=m),
    )
    hook(_request())
    assert seen["env"].actor_context.roles == ("operator", "Researcher")


class _Directory:
    name = "local"

    def __init__(self, groups):
        self._groups = groups

    def groups_for(self, principal):
        from axiom.extensions.builtins.directory.protocol import GroupRef

        return [GroupRef(id=g, provider=self.name) for g in self._groups]


def test_directory_answer_is_authoritative_over_the_token_roles_claim():
    seen, decide_fn = _capture()
    m = _membership(
        provider=_Directory(["grp-students"]), rules=[{"group": "grp-students", "role": "student"}]
    )
    hook = build_authz_hook(
        resolve_principal=lambda r: _cred(
            claims={"sub": "oid-1", "roles": ["Admin"]}
        ),  # no groups claim
        decide_fn=decide_fn,
        resolve_actor_context=build_actor_resolver(membership=m),
    )
    hook(_request())
    actor = seen["env"].actor_context
    assert actor.roles == ("student",)  # the token's Admin does not survive the directory
    assert actor.attributes["membership_source"] == "directory"


def test_degraded_membership_yields_no_contextual_tuples_but_reports_it():
    seen, decide_fn = _capture()

    class Down:
        name = "local"

        def groups_for(self, principal):
            raise ConnectionError("directory unreachable")

    m = _membership(provider=Down(), rules=[{"group": "*", "role": "*"}])
    hook = build_authz_hook(
        resolve_principal=lambda r: _cred(
            claims={"sub": "oid-1", "_claim_names": {"groups": "src1"}, "groups": ["grp-x"]}
        ),
        decide_fn=decide_fn,
        resolve_actor_context=build_actor_resolver(membership=m),
    )
    hook(_request())
    env = seen["env"]
    assert env.actor_context.attributes["membership_stale"] is True
    assert env.actor_context.attributes["membership_source"] == "claims-degraded"
    assert env.subject.contextual_tuples == ()  # no grants from an unconfirmable projection
    assert env.subject.attributes["membership_stale"] is True


def test_revoked_group_is_dropped_even_when_the_token_still_asserts_it():
    seen, decide_fn = _capture()
    revoked = RevokedSet()
    revoked.record("oid-1", "grp-operators")
    m = _membership(rules=[{"group": "grp-operators", "role": "operator"}], revoked=revoked)
    hook = build_authz_hook(
        resolve_principal=lambda r: _cred(claims={"sub": "oid-1", "groups": ["grp-operators"]}),
        decide_fn=decide_fn,
        resolve_actor_context=build_actor_resolver(membership=m),
    )
    hook(_request())
    assert seen["env"].actor_context.roles == ()
    assert seen["env"].subject.contextual_tuples == ()


# ---------------------------------------------------------------- role-scoped rules


def test_role_scoped_rule_matches_only_when_the_actor_holds_the_role():
    from dataclasses import replace

    from axiom.extensions.builtins.authz.rules import Rule
    from axiom.extensions.builtins.http.authz_hook import default_envelope_builder
    from axiom.governance import IntentPattern, ResourcePattern
    from axiom.governance.actor import ActorContext

    env = default_envelope_builder(_request(), _principal("@alice:site"))
    rule = Rule(
        name="operators-invoke",
        intent_pattern=IntentPattern("http.*"),
        actor_pattern="*",
        resource_pattern=ResourcePattern("extension://*"),
        roles=frozenset({"operator"}),
    )
    assert rule.matches(env) is False  # no actor context → fail-closed
    assert (
        rule.matches(
            replace(env, actor_context=ActorContext(handle="@alice:site", roles=("student",)))
        )
        is False
    )
    assert (
        rule.matches(
            replace(env, actor_context=ActorContext(handle="@alice:site", roles=("operator",)))
        )
        is True
    )
    # an unscoped rule is unchanged
    open_rule = replace(rule, roles=frozenset())
    assert open_rule.matches(env) is True


# ---------------------------------------------------------------- session cookie resolver


def test_session_cookie_resolves_to_principal_with_claims_and_posture():
    user = User(
        user_id="8f2d0c1e-5a3b-4c6d-9e7f-0123456789ab",
        email="Alice@Example.edu",
        name="Alice",
        roles=("Researcher",),
        attributes={"idp": "entra"},
    )
    token = issue_session_token(user, ttl=timedelta(hours=1), issuer="http://node.example")
    resolver = build_session_resolver(context="ut-triga")
    cred = resolver(_request(cookies={SESSION_COOKIE: token}))
    assert cred is not None
    assert cred.principal.handle == "@8f2d0c1e-5a3b-4c6d-9e7f-0123456789ab:ut-triga"
    assert cred.posture == "sso"
    assert cred.claims["roles"] == ["Researcher"]
    assert cred.claims["email"] == "alice@example.edu"
    assert cred.claims["idp"] == "entra"
    assert cred.credential_id == "session:8f2d0c1e-5a3b-4c6d-9e7f-0123456789ab"


def test_password_session_is_attested_not_sso_and_email_subjects_are_slugged():
    user = User(user_id="alice@example.edu", email="alice@example.edu", roles=("operator",))
    token = issue_session_token(user, ttl=timedelta(hours=1), issuer="http://node.example")
    cred = build_session_resolver()(_request(cookies={SESSION_COOKIE: token}))
    assert cred.posture == "attested"
    assert cred.principal.handle == "@alice_example.edu:gate"
    assert cred.claims["sub"] == "alice@example.edu"  # the real subject survives in claims


def test_session_resolver_refuses_wrong_issuer_and_non_session_tokens():
    user = User(user_id="u1", email="u@x.org")
    foreign = issue_session_token(user, ttl=timedelta(hours=1), issuer="http://other.example")
    assert build_session_resolver()(_request(cookies={SESSION_COOKIE: foreign})) is None
    access = create_access_token(
        {"sub": "u1"}, expires_delta=timedelta(hours=1), issuer="http://node.example"
    )
    assert build_session_resolver()(_request(cookies={SESSION_COOKIE: access})) is None
    assert build_session_resolver()(_request()) is None


def test_chain_prefers_session_over_bearer_and_falls_back():
    user = User(user_id="u1", email="u@x.org", roles=("operator",))
    token = issue_session_token(user, ttl=timedelta(hours=1), issuer="http://node.example")
    bearer = build_bearer_resolver({"legacy-token": "@svc:org"})
    chain = chain_resolvers(build_session_resolver(), bearer)
    both = _request(
        cookies={SESSION_COOKIE: token}, headers={"authorization": "Bearer legacy-token"}
    )
    assert chain(both).principal.handle == "@u1:gate"
    only_bearer = _request(headers={"authorization": "Bearer legacy-token"})
    assert chain(only_bearer).handle == "@svc:org"
    assert chain(_request()) is None


def test_end_to_end_session_roles_reach_decide():
    seen, decide_fn = _capture()
    user = User(user_id="oid-9", email="op@x.org", roles=("operator",), attributes={"idp": "entra"})
    token = issue_session_token(user, ttl=timedelta(hours=1), issuer="http://node.example")
    hook = build_authz_hook(
        resolve_principal=build_session_resolver(context="site"), decide_fn=decide_fn
    )
    req = _request(cookies={SESSION_COOKIE: token})
    assert hook(req).allow is True
    actor = seen["env"].actor_context
    assert actor.handle == "@oid-9:site"
    assert actor.roles == ("operator",)
    assert actor.assurance.posture == "sso"
    assert seen["env"].subject.fga_user == "user:oid-9"


# ---------------------------------------------------------------- env wiring


def test_slug_folds_subjects_into_the_handle_grammar():
    assert slug("alice@example.edu") == "alice_example.edu"
    assert slug("8f2d-0c1e") == "8f2d-0c1e"
    assert slug("") == "anon"
    assert slug("__x__") == "x"


def test_actor_resolver_from_env_defaults_to_claims_first(tmp_path):
    seen, decide_fn = _capture()
    role_map = tmp_path / "roles.json"
    role_map.write_text('{"rules": [{"group": "grp-a", "role": "alpha"}]}')
    resolver = actor_resolver_from_env(
        {"AXIOM_DIRECTORY_ROLE_MAP": str(role_map), "AXIOM_TENANT": "ut-triga"}
    )
    hook = build_authz_hook(
        resolve_principal=lambda r: _cred(
            claims={"sub": "s", "groups": ["grp-a"], "tid": "ignored"}
        ),
        decide_fn=decide_fn,
        resolve_actor_context=resolver,
    )
    hook(_request())
    actor = seen["env"].actor_context
    assert actor.roles == ("alpha",)
    assert actor.tenant == "ut-triga"  # configured tenant wins over the token's tid


def test_actor_resolver_from_env_local_provider(tmp_path):
    seen, decide_fn = _capture()
    directory = tmp_path / "dir.json"
    directory.write_text('{"groups": {"grp-ops": {"members": ["s"]}}}')
    role_map = tmp_path / "roles.json"
    role_map.write_text('{"rules": [{"group": "grp-ops", "role": "operator"}]}')
    resolver = actor_resolver_from_env(
        {
            "AXIOM_DIRECTORY_PROVIDER": "local",
            "AXIOM_DIRECTORY_LOCAL_FILE": str(directory),
            "AXIOM_DIRECTORY_ROLE_MAP": str(role_map),
        }
    )
    hook = build_authz_hook(
        resolve_principal=lambda r: _cred(claims={"sub": "s"}),
        decide_fn=decide_fn,
        resolve_actor_context=resolver,
    )
    hook(_request())
    assert seen["env"].actor_context.roles == ("operator",)
    assert seen["env"].actor_context.attributes["membership_source"] == "directory"


def test_actor_resolver_from_env_misconfiguration_falls_back_to_claims_only():
    seen, decide_fn = _capture()
    resolver = actor_resolver_from_env({"AXIOM_DIRECTORY_PROVIDER": "entra"})  # not wired here
    hook = build_authz_hook(
        resolve_principal=lambda r: _cred(
            claims={"sub": "s", "roles": ["Researcher"], "groups": ["grp-a"]}
        ),
        decide_fn=decide_fn,
        resolve_actor_context=resolver,
    )
    hook(_request())
    actor = seen["env"].actor_context
    assert actor.roles == ("Researcher",)  # claims only; groups not mapped
    assert "membership_source" not in actor.attributes
