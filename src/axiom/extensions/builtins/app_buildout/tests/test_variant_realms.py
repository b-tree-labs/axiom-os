# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VAR-A2: a session from one realm is refused by the other two.

Run through the real session layer (``axiom.webauth.session``), which every
served surface reads: the token is a signed ES256 JWT with the realm inside
it, and each variant reads its own cookie. The browser sends every cookie to
every path on one host, so a separate cookie name alone is not isolation; the
signed realm is.
"""

from __future__ import annotations

import pytest

from axiom.infra import variants as v
from axiom.webauth import session as s
from axiom.webauth.users import User


@pytest.fixture(autouse=True)
def _keys(tmp_path, monkeypatch):
    # A fresh ephemeral signing key for this test, never a cached one.
    from axiom.webauth.keys import reset_key_store_for_tests

    reset_key_store_for_tests()
    monkeypatch.setenv(v.CONFIG_ENV, str(tmp_path / "absent.toml"))
    for name in (s.REALM_ENV, s.COOKIE_ENV, s.LEGACY_ENV, v.VARIANT_ENV):
        monkeypatch.delenv(name, raising=False)


def _as(monkeypatch, name: str) -> v.Variant:
    variant = v.get_variant(name)
    for key in (s.REALM_ENV, s.COOKIE_ENV, s.LEGACY_ENV):
        monkeypatch.delenv(key, raising=False)
    for key, value in variant.child_env().items():
        monkeypatch.setenv(key, value)
    return variant


def _sign_in(monkeypatch, name: str) -> dict[str, str]:
    """What the browser holds after signing in to ``name``: its cookie."""
    variant = _as(monkeypatch, name)
    user = User(user_id="@ada:example", email="ada@example.org", name="Ada", roles=("viewer",))
    return {variant.cookie: s.issue_session_token(user)}


@pytest.mark.parametrize("home", v.NAMES)
def test_var_a2_a_session_is_honoured_in_its_own_realm(monkeypatch, home):
    jar = _sign_in(monkeypatch, home)
    _as(monkeypatch, home)
    claims = s.session_from_cookies(jar)
    assert claims and claims["sub"] == "@ada:example"
    assert claims["realm"] == v.get_variant(home).realm


@pytest.mark.parametrize(
    "home,other", [(h, o) for h in v.NAMES for o in v.NAMES if h != o]
)
def test_var_a2_a_session_from_one_realm_is_refused_by_the_other_two(monkeypatch, home, other):
    jar = _sign_in(monkeypatch, home)
    _as(monkeypatch, other)
    # Read under the other variant's cookie name too: the cookie is not the guard.
    replayed = {v.get_variant(other).cookie: next(iter(jar.values()))}
    assert s.session_from_cookies(jar) is None
    assert s.session_from_cookies(replayed) is None


def test_var_a2_all_three_signed_in_at_once_each_reads_only_its_own(monkeypatch):
    jar: dict[str, str] = {}
    for name in v.NAMES:
        jar.update(_sign_in(monkeypatch, name))
    for name in v.NAMES:
        _as(monkeypatch, name)
        assert s.session_from_cookies(jar)["realm"] == v.get_variant(name).realm


def test_var_a2_the_site_takes_over_an_existing_session_in_place(monkeypatch):
    """No forced re-auth: the plain install's session still works on the site variant."""
    user = User(user_id="@ada:example", email="ada@example.org", name="Ada", roles=())
    legacy = {s.SESSION_COOKIE: s.issue_session_token(user)}  # minted before any variant
    _as(monkeypatch, "site")
    assert s.session_from_cookies(legacy)["sub"] == "@ada:example"
    for bare in ("axiom", "product"):
        _as(monkeypatch, bare)
        assert s.session_from_cookies(legacy) is None


def test_var_a2_a_plain_install_refuses_a_variant_session(monkeypatch):
    jar = _sign_in(monkeypatch, "axiom")
    for key in (s.REALM_ENV, s.COOKIE_ENV, s.LEGACY_ENV):
        monkeypatch.delenv(key, raising=False)
    assert s.session_from_cookies({s.SESSION_COOKIE: next(iter(jar.values()))}) is None


def test_var_a2_the_gate_sets_and_clears_the_variants_cookie(monkeypatch):
    _as(monkeypatch, "product")
    assert s.session_cookie_name() == "axiom_s_product"
    for key in (s.COOKIE_ENV,):
        monkeypatch.delenv(key)
    assert s.session_cookie_name() == s.SESSION_COOKIE
