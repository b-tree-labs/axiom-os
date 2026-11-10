"""A key granted under an extension's former name still reaches the mount.

A consumer renamed its telemetry extension, and every key issued with the old
name in its scope was refused from the moment the node upgraded. Nobody had
done anything wrong: the grant was right when issued, and the resource it named
had simply been given a new name. A mount now declares its former names, and a
scope that names one covers it. Nothing else about the grant changes: the verb
still has to match, and an alias never widens a key to another mount.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from axiom.governance import Decision, Verdict
from axiom.webauth.api_keys import JsonFileApiKeyStore, append_api_key_record, mint_api_key


def _key(tmp_path: Path, scopes):
    f = tmp_path / "keys.json"
    token, record = mint_api_key(principal="@svc:org", scopes=scopes)
    append_api_key_record(f, record)
    return token, JsonFileApiKeyStore(f)


def _hook(store):
    from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver

    return build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=store),
        decide_fn=lambda env: Verdict.from_decision(Decision.PERMIT, "ok", "r"),
    )


def _request(token, *, method="GET", aliases=("old_name",)):
    spec = SimpleNamespace(scope_aliases=aliases, governs_path=lambda p: False)
    state = SimpleNamespace(mount_extension="new_name", mount_spec=spec)
    return SimpleNamespace(state=state, method=method, url=SimpleNamespace(path="/new/metrics"),
                           headers={"authorization": f"Bearer {token}"})


def test_a_scope_naming_the_former_name_covers_the_mount(tmp_path):
    token, store = _key(tmp_path, ("old_name:read",))
    assert _hook(store)(_request(token)).allow is True


def test_the_verb_still_has_to_match(tmp_path):
    token, store = _key(tmp_path, ("old_name:read",))
    assert _hook(store)(_request(token, method="POST")).allow is False


def test_a_mount_that_declares_no_alias_refuses_the_old_name(tmp_path):
    token, store = _key(tmp_path, ("old_name:read",))
    assert _hook(store)(_request(token, aliases=())).allow is False


def test_an_alias_never_reaches_another_mount(tmp_path):
    token, store = _key(tmp_path, ("unrelated:read",))
    assert _hook(store)(_request(token)).allow is False


def test_the_current_name_still_works(tmp_path):
    token, store = _key(tmp_path, ("new_name:read",))
    assert _hook(store)(_request(token)).allow is True


def test_a_mount_spec_declares_aliases():
    from fastapi import APIRouter

    from axiom.extensions.builtins.http.registry import MountSpec

    spec = MountSpec(prefix="/new", router=APIRouter(), extension="new_name",
                     scope_aliases=("old_name",))
    assert spec.scope_aliases == ("old_name",)


def test_the_capability_names_the_current_mount_and_covers_the_request(tmp_path):
    from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver

    token, store = _key(tmp_path, ("old_name:read",))
    seen = {}

    def decide(env):
        assert env.capability.permits_intent(env.intent)
        assert env.capability.permits_resource(env.resource)
        seen["resource"] = env.capability.resource_pattern.value
        return Verdict.from_decision(Decision.PERMIT, "ok", "r")

    hook = build_authz_hook(resolve_principal=build_bearer_resolver({}, api_keys=store), decide_fn=decide)
    assert hook(_request(token)).allow is True
    assert seen["resource"] == "extension://new_name/*"
