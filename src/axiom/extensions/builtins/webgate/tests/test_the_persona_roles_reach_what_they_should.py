# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Each persona role reaches what a person in it came to do, and nothing more (ADR-222).

The first colleague who signed in made a self-service key and every data read answered 403: the
role read, and a data question arrives as POST. The fix was one declaration, and the lesson was
that nobody had asked a role to do the job of the person holding it. So this asks, for each of
the three persona roles, with a key minted by the gate's real self-service route and presented to
the real authz middleware:

- **reader**: data, documents and chat; no kit submission, no governance;
- **contributor**: a reader who also submits a kit;
- **site-admin**: a contributor who also governs (the review queue).

The same check is the release check (``python -m axiom.extensions.builtins.webgate.role_check``),
run by the publish workflow, and the hook a consumer's onboarding job calls with ``--role``.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.webgate import role_check  # noqa: E402
from axiom.extensions.builtins.webgate.role_bundles import (  # noqa: E402
    PERSONA_ROLES,
    ScopeBundle,
    _covers,
    default_bundle_registry,
    jit_default_role,
)

ROLES = ("reader", "contributor", "site-admin")


@pytest.mark.parametrize("role", ROLES)
def test_the_persona_roles_reach_what_they_should(role):
    [report] = role_check.check_in_process([role])
    assert report.ok, "\n" + role_check.render([report])
    reached = {r.probe.act for r in report.results if r.outcome == "reached"}
    refused = {r.probe.act for r in report.results if r.outcome == "refused"}
    assert reached == role_check.REACHES[role]
    assert refused == set(role_check.ACTS) - role_check.REACHES[role]


def test_the_matrix_is_the_one_the_plan_names():
    """Written out, so a change to the matrix is a change somebody reads."""
    planned = {
        "reader": {"data read", "documents", "chat"},
        "contributor": {"data read", "documents", "chat", "kit submit"},
        "site-admin": {"data read", "documents", "chat", "kit submit", "governance"},
    }
    assert planned == role_check.REACHES


def test_each_role_holds_everything_the_one_below_it_holds():
    """A ladder, so moving somebody up never takes a permission away."""
    registry = default_bundle_registry()
    assert PERSONA_ROLES == ROLES
    for lower, upper in zip(PERSONA_ROLES, PERSONA_ROLES[1:]):
        lo, up = registry.get(lower), registry.get(upper)
        for scope in lo.scopes:
            assert any(_covers(granted, scope) for granted in up.scopes), (
                f"{upper} lacks {scope}, which {lower} holds")


def test_a_role_that_cannot_chat_fails_the_check():
    """The negative control: the check must be able to fail. A reader narrowed to ADR-151's
    ``viewer`` grant is exactly the role the first colleague had, and it cannot chat."""
    registry = default_bundle_registry()
    original = registry.get("reader")
    registry.register(ScopeBundle(role="reader", scopes=("*:read",)), replace=True)
    try:
        [report] = role_check.check_in_process(["reader"])
    finally:
        registry.register(original, replace=True)
    assert not report.ok
    assert any("should reach a chat completion" in p for p in report.problems), report.problems


def test_a_role_that_reaches_too_far_fails_the_check():
    registry = default_bundle_registry()
    original = registry.get("contributor")
    registry.register(ScopeBundle(role="contributor", scopes=("*:read", "*:invoke", "*:govern")),
                      replace=True)
    try:
        [report] = role_check.check_in_process(["contributor"])
    finally:
        registry.register(original, replace=True)
    assert any("must not reach the review queue" in p for p in report.problems), report.problems


def test_the_first_sign_in_grants_reader(monkeypatch):
    monkeypatch.delenv("AXIOM_GATE_JIT_ROLE", raising=False)
    assert jit_default_role() == "reader"


def test_the_review_queue_is_governance_whatever_its_method():
    from axiom.extensions.builtins.data_platform.services.read_mount import mount_spec
    from axiom.extensions.builtins.http.authz_hook import default_envelope_builder
    from axiom.vega.identity.principal import Principal

    spec = mount_spec()
    who = Principal(handle="@someone:example-loop", public_bytes=hashlib.sha256(b"x").digest())
    for method in ("GET", "POST"):
        request = SimpleNamespace(method=method, url=SimpleNamespace(path="/data/review"),
                                  state=SimpleNamespace(mount_extension=spec.extension, mount_spec=spec))
        assert str(default_envelope_builder(request, who).intent) == "http.govern"


def test_a_sited_site_admin_reviews_only_its_own_sites_queue(monkeypatch):
    """Served, the review queue is held to the credential's site like every other kit read."""
    from axiom.extensions.builtins.data_platform.kit import host
    from axiom.extensions.builtins.data_platform.skills import kit_promotion

    asked: dict = {}
    monkeypatch.setattr(kit_promotion, "_caller", lambda: ("example-loop", False))
    monkeypatch.setattr(kit_promotion, "_node_dsn", lambda: "dbname=stub")
    monkeypatch.setattr(host, "review", lambda dsn, site=None, state=None: asked.setdefault("site", site) and [])
    monkeypatch.setattr(host, "withdrawals", lambda dsn, site=None: [])
    ok = kit_promotion.kit_review({"_principal": "@op:example-loop"}, None)
    assert ok.ok and asked["site"] == "example-loop"
    other = kit_promotion.kit_review({"_principal": "@op:example-loop", "site": "another-site"}, None)
    assert not other.ok and "another-site" in other.errors[0]


# ------------------------------------------------------------------- the hook for a live node


class _FakeNode:
    """Answers each probe the way a node holding the given scopes would."""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.seen: list[tuple[str, str]] = []

    def __call__(self, method, url, *, headers=None, data=None, timeout=30.0):
        path = "/" + url.split("/", 3)[3]
        self.seen.append((method, path))
        assert headers["authorization"].startswith("Bearer "), "the key was not presented"
        act = next(p.act for p in role_check.PROBES if p.path == path and p.method == method)
        return (200 if act in self.allowed else 403), {}, b"{}"


def test_the_live_check_judges_a_node_by_its_answers(monkeypatch):
    monkeypatch.setattr(role_check, "_http", _FakeNode({"data read", "documents", "chat"}))
    report = role_check.check_live("reader", "http://node.example.org", "axk_not-a-real-key")
    assert report.ok, report.problems
    monkeypatch.setattr(role_check, "_http", _FakeNode({"data read", "documents"}))
    report = role_check.check_live("reader", "http://node.example.org", "axk_not-a-real-key")
    assert not report.ok


def test_a_door_the_node_does_not_serve_is_said_and_not_passed(monkeypatch):
    def older_node(method, url, **kw):
        return (404 if url.endswith("/data/review") else 200 if "submit" not in url else 403), {}, b"{}"

    monkeypatch.setattr(role_check, "_http", older_node)
    report = role_check.check_live("reader", "http://node.example.org", "axk_x")
    assert report.ok
    review = next(r for r in report.results if r.probe.path == "/data/review")
    assert review.outcome == "not served"


def test_the_command_line_takes_a_role_and_never_prints_the_key(monkeypatch, capsys):
    monkeypatch.setattr(role_check, "_http", _FakeNode({"data read", "documents", "chat", "kit submit"}))
    monkeypatch.setenv("ROLE_KEY", "axk_secret-value-that-must-not-print")
    code = role_check.main(["--role", "contributor", "--node", "http://node.example.org",
                            "--key-env", "ROLE_KEY", "--json"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "secret-value" not in out


def test_the_command_line_fails_when_a_role_is_wrong(capsys):
    registry = default_bundle_registry()
    original = registry.get("reader")
    registry.register(ScopeBundle(role="reader", scopes=("*:read",)), replace=True)
    try:
        assert role_check.main(["--role", "reader"]) == 1
    finally:
        registry.register(original, replace=True)
    assert "WRONG" in capsys.readouterr().out
