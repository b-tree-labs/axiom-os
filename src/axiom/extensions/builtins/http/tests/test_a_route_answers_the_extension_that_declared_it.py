# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The gate's rule for a path a mount says another extension owns (#1777).

A scope naming the owner covers the owned path and what is below it, by path
segment, with the same verb rule. The decision is still made on the mount's own
address, and the capability that rides it covers only the owned subpath.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter

from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
from axiom.extensions.builtins.http.registry import MountSpec
from axiom.governance import Decision, Verdict

SPEC = MountSpec(prefix="/api/v1", router=APIRouter(), extension="webapp",
                 owners=(("/api/v1/fleet", "fleet"),))


class _Keys:
    def __init__(self, scopes):
        self._scopes = scopes

    def resolve(self, token):
        return SimpleNamespace(principal="@svc:example-site", scopes=self._scopes,
                               key_id="k1", name="k", site="example-site")


def _decide(seen):
    def decide(env):
        seen["resource"] = str(env.resource)
        seen["capability"] = env.capability.resource_pattern.value
        assert env.capability.permits_resource(env.resource)
        return Verdict.from_decision(Decision.PERMIT, "ok", "r")
    return decide


def _ask(scopes, path, *, method="POST", seen=None):
    hook = build_authz_hook(resolve_principal=build_bearer_resolver({}, api_keys=_Keys(scopes)),
                            decide_fn=_decide(seen if seen is not None else {}))
    state = SimpleNamespace(mount_extension="webapp", mount_spec=SPEC)
    request = SimpleNamespace(state=state, method=method, url=SimpleNamespace(path=path),
                              headers={"authorization": "Bearer axk_test"})
    return hook(request)


def test_the_owner_covers_its_path_and_below():
    assert _ask(("fleet:invoke",), "/api/v1/fleet/reports").allow is True
    assert _ask(("fleet:invoke",), "/api/v1/fleet").allow is True


def test_a_path_that_only_starts_with_the_same_letters_is_not_owned():
    assert SPEC.owner_of("/api/v1/fleetx/reports") is None
    assert _ask(("fleet:invoke",), "/api/v1/fleetx/reports").allow is False


def test_the_owner_reaches_nothing_else_on_the_mount():
    assert _ask(("fleet:invoke",), "/api/v1/sites").allow is False
    assert _ask(("fleet",), "/api/v1/receipts/today", method="GET").allow is False


def test_the_verb_still_has_to_match():
    assert _ask(("fleet:read",), "/api/v1/fleet/reports").allow is False


def test_the_decision_keeps_the_mounts_address_and_a_narrow_capability():
    seen = {}
    assert _ask(("fleet:invoke",), "/api/v1/fleet/reports", seen=seen).allow is True
    assert seen["resource"] == "extension://webapp/api/v1/fleet/reports"
    assert seen["capability"] == "extension://webapp/api/v1/fleet/*"


def test_an_owned_path_must_be_a_path():
    import pytest

    with pytest.raises(ValueError):
        MountSpec(prefix="/api/v1", router=APIRouter(), extension="webapp",
                  owners=(("api/v1/fleet", "fleet"),))
