# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A mount says which of its routes govern — ADR-151 §2.

`govern` cannot be derived from the request. POST is `invoke` whether it
writes a document or grants somebody a role, and GET is `read` whether it
returns a measurement or the audit trail of who was granted what. The
method knows the shape of the call and nothing about what the call means.

So the owning extension declares, and until it does, its governing routes
are indistinguishable from its writing ones and `admin` cannot be enforced
for it. `admin` reading nothing is a promise that this is what keeps.

Reading a governing route IS governing: listing who holds a role, or
reading an audit trail, is an act of governance however it arrives. A
`viewer` therefore cannot read them, which is the intended consequence
rather than an oversight.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from axiom.extensions.builtins.http.authz_hook import default_envelope_builder
from axiom.extensions.builtins.http.registry import MountSpec
from axiom.vega.identity.principal import Principal


def _request(path: str, method: str, *, governs: tuple[str, ...] = ()) -> SimpleNamespace:
    """A request as the middleware leaves it: the mount it resolved to is
    already on `request.state`, which is why this needs no new plumbing.

    The spec is a real `MountSpec` rather than a stand-in, so the matching
    under test is the matching that ships.
    """
    spec = MountSpec(
        prefix="/agreements",
        router=None,  # type: ignore[arg-type]
        extension="agreements",
        governs=governs,
    )
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        state=SimpleNamespace(mount_extension="agreements", mount_spec=spec),
    )


def _who() -> Principal:
    handle = "@someone"
    return Principal(handle=handle, public_bytes=hashlib.sha256(handle.encode()).digest())


def _intent(path: str, method: str, governs: tuple[str, ...] = ()) -> str:
    return default_envelope_builder(_request(path, method, governs=governs), _who()).intent.value


def test_a_mount_that_declares_nothing_behaves_as_it_always_did():
    assert _intent("/agreements/list", "GET") == "http.read"
    assert _intent("/agreements/sign", "POST") == "http.invoke"


def test_a_declared_route_governs_whatever_the_method_is():
    governs = ("/agreements/admit",)
    assert _intent("/agreements/admit", "POST", governs) == "http.govern"
    assert _intent("/agreements/admit", "GET", governs) == "http.govern"
    assert _intent("/agreements/admit", "DELETE", governs) == "http.govern"


def test_reading_a_governing_route_is_governing():
    """Listing who holds a grant is an act of governance, so a `viewer`
    cannot do it. Intended, not an oversight."""
    assert _intent("/agreements/grants", "GET", ("/agreements/grants",)) == "http.govern"


def test_a_sibling_route_under_the_same_mount_is_untouched():
    governs = ("/agreements/admit",)
    assert _intent("/agreements/list", "GET", governs) == "http.read"
    assert _intent("/agreements/sign", "POST", governs) == "http.invoke"


def test_a_prefix_matches_what_is_under_it_and_not_a_lookalike():
    governs = ("/agreements/admit",)
    assert _intent("/agreements/admit/bulk", "POST", governs) == "http.govern"
    # `/admittance` starts with the same characters and is a different route.
    assert _intent("/agreements/admittance", "POST", governs) == "http.invoke"


def test_the_declaration_is_a_field_on_the_mount_so_it_travels_with_it():
    """Declared beside the router rather than in a table somewhere else: a
    mount that moves takes its declaration with it, and a reviewer sees both
    in one diff."""
    spec = MountSpec(
        prefix="/agreements",
        router=None,  # type: ignore[arg-type]
        extension="agreements",
        governs=("/agreements/admit",),
    )
    assert spec.governs == ("/agreements/admit",)


def test_an_undeclared_mount_still_has_the_field_and_it_is_empty():
    spec = MountSpec(prefix="/x", router=None, extension="x")  # type: ignore[arg-type]
    assert spec.governs == ()


# ---------------------------------------------------------------------------
# End to end, through the real hook: the declaration is what makes ADR-151's
# central claim true. `admin` governs and reads nothing; `creator` reads and
# writes and governs nothing. Neither sentence means anything until a route
# can be classified, so this is the guard for the whole mechanism rather than
# for the matcher.
# ---------------------------------------------------------------------------

GOVERNING = "/agreements/admit"
ORDINARY = "/agreements/sign"


def _keyed(tmp_path, scopes: tuple[str, ...]):
    """A real issued key with real scopes, and the real hook over it."""
    from axiom.extensions.builtins.http.authz_hook import (
        build_authz_hook,
        build_bearer_resolver,
    )
    from axiom.governance import Decision, Verdict
    from axiom.webauth.api_keys import (
        JsonFileApiKeyStore,
        append_api_key_record,
        mint_api_key,
    )

    f = tmp_path / "api-keys.json"
    token, record = mint_api_key(principal="@who:org", scopes=scopes)
    append_api_key_record(f, record)
    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=JsonFileApiKeyStore(f)),
        decide_fn=lambda env: Verdict.from_decision(Decision.PERMIT, "allowed", "rcpt"),
    )
    return token, hook


def _ask(hook, token, path: str, method: str) -> bool:
    req = _request(path, method, governs=(GOVERNING,))
    req.headers = {"authorization": f"Bearer {token}"}
    return hook(req).allow


def test_admin_governs_and_reads_nothing_else(tmp_path):
    token, hook = _keyed(tmp_path, ("*:govern",))
    assert _ask(hook, token, GOVERNING, "POST") is True
    assert _ask(hook, token, GOVERNING, "GET") is True
    # The role's whole point: the rest of the surface is closed to it.
    assert _ask(hook, token, ORDINARY, "GET") is False
    assert _ask(hook, token, ORDINARY, "POST") is False


def test_creator_reads_and_writes_and_governs_nothing(tmp_path):
    token, hook = _keyed(tmp_path, ("*:read", "*:invoke"))
    assert _ask(hook, token, ORDINARY, "GET") is True
    assert _ask(hook, token, ORDINARY, "POST") is True
    # Holding every ordinary verb is not holding this one.
    assert _ask(hook, token, GOVERNING, "POST") is False
    assert _ask(hook, token, GOVERNING, "GET") is False


def test_without_the_declaration_a_creator_reaches_the_governing_route(tmp_path):
    """The negative control, and the reason a silent mount is the hazard.

    Same key, same path, declaration removed: it goes through. So an
    extension that governs something and declares nothing is not
    half-protected, it is unprotected, and nothing in the response says so.
    """
    token, hook = _keyed(tmp_path, ("*:read", "*:invoke"))
    undeclared = _request(GOVERNING, "POST", governs=())
    undeclared.headers = {"authorization": f"Bearer {token}"}
    assert hook(undeclared).allow is True


def test_owner_still_holds_everything(tmp_path):
    """A new verb must not punch a hole in the one role that holds all of
    them. `*` is every verb, which has to keep including the new one."""
    token, hook = _keyed(tmp_path, ("*",))
    assert _ask(hook, token, GOVERNING, "POST") is True
    assert _ask(hook, token, ORDINARY, "POST") is True


def test_the_route_listing_says_which_paths_govern():
    """Visible in `axi serve --list`, because a mount that governs something
    and declares nothing looks exactly like one that governs nothing."""
    from axiom.extensions.builtins.http.compose import RouteTableEntry

    row = RouteTableEntry(
        prefix="/agreements",
        extension="agreements",
        requires_authz=True,
        profiles=(),
        trust_zone=None,
        governs=(GOVERNING,),
    )
    assert row.governs == (GOVERNING,)


def test_a_declaration_that_could_never_match_is_refused():
    """A path with no leading slash matches no request, so it reads as
    governed and is not. Loud at construction beats silent at runtime."""
    import pytest

    with pytest.raises(ValueError) as refused:
        MountSpec(
            prefix="/x",
            router=None,  # type: ignore[arg-type]
            extension="x",
            governs=("admit",),
        )
    assert "governs" in str(refused.value)
