# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`vault resolve` — which credential, of the several for this host?

The confusion this exists to end, from a real session: one host carries seven
credentials with different scopes. The git-credential helper is keyed by HOST
alone, so asking it for "the credential for rsicc-gitlab" silently returned the
git-scoped mirror token when the API-scoped one was needed. The call 401'd, and
the wrong conclusion drawn was "there is no working credential" — while a
perfectly good one sat in the store under a different name.

A helper that silently picks one of seven is the bug. `resolve` reports ALL of
them with scope, purpose and health, says which one the helper would hand over,
and refuses to choose when nothing distinguishes them. Never a value.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.extensions.builtins.vault.skills import resolve as resolve_skill
from axiom.infra.skills import SkillContext, default_registry


#: Distinctive on purpose. Asserting a short string like "x" is absent from
#: a JSON blob passes or fails on unrelated words like "expires_at" — a
#: leak test that cannot fail is worse than none.
SECRET = b"SUPERSECRET-VALUE-9f3c1"


@pytest.fixture
def ctx(tmp_path):
    import logging

    store = ForeignCredentialStore(tmp_path)
    store.set("gl-git", SECRET, provider="gitlab-pat",
              git_host="git.example.org", issuer_url="https://git.example.org",
              notes="git-only scopes: read_repository/write_repository")
    store.set("gl-api", SECRET, provider="gitlab-pat",
              issuer_url="https://git.example.org", expires_at="2027-11-30",
              notes="api scope — program-tracker automation")
    store.set("other-host", SECRET, provider="guided",
              issuer_url="https://elsewhere.example.org")
    return SkillContext(registry=default_registry(), state_dir=tmp_path,
                        logger=logging.getLogger("t"))


def test_every_candidate_for_a_host_is_listed(ctx):
    """Listing one is the failure. Seeing that a host has several is what stops
    the wrong one being used without anybody noticing."""
    result = resolve_skill.run({"host": "git.example.org"}, ctx)
    assert result.ok
    names = {c["name"] for c in result.value["candidates"]}
    assert names == {"gl-git", "gl-api"}
    assert "other-host" not in names


def test_it_names_the_one_the_git_helper_would_hand_over(ctx):
    """The silent pick, made loud. Someone reaching for a credential through
    git's helper gets exactly one, and until now nothing said which."""
    result = resolve_skill.run({"host": "git.example.org"}, ctx)
    assert result.value["git_helper_would_return"] == "gl-git"


def test_health_travels_with_each_candidate(ctx):
    """'Which one' and 'is it alive' are the same question in practice — the
    session that prompted this picked a credential that was also expired."""
    result = resolve_skill.run({"host": "git.example.org"}, ctx)
    by_name = {c["name"]: c for c in result.value["candidates"]}
    assert by_name["gl-git"]["health"] == "no_expiry"
    assert by_name["gl-api"]["health"] == "ok"
    assert by_name["gl-api"]["expires_at"] == "2027-11-30"


def test_a_purpose_narrows_to_one(ctx):
    result = resolve_skill.run({"host": "git.example.org", "purpose": "api"}, ctx)
    assert result.value["resolved"] == "gl-api"


def test_an_unmatched_purpose_refuses_rather_than_guessing(ctx):
    """Returning a best guess is how the original mistake happened. When
    nothing declares the purpose asked for, say so and list what exists."""
    result = resolve_skill.run({"host": "git.example.org", "purpose": "kubernetes"}, ctx)
    assert result.ok is False
    assert result.value["resolved"] is None
    assert result.value["candidates"], "refused without saying what DOES exist"


def test_no_credential_value_is_ever_returned(ctx):
    """The whole point is to be safe to call from chat, MCP and a transcript.
    A resolver that leaked values could not be exposed on any of them."""
    import json

    result = resolve_skill.run({"host": "git.example.org"}, ctx)
    blob = json.dumps(result.value)
    assert "password" not in blob and "value" not in blob
    assert SECRET.decode() not in blob


def test_an_unknown_host_says_so_with_the_hosts_it_knows(ctx):
    """A bare 'not found' sends the caller back to guessing."""
    result = resolve_skill.run({"host": "nope.example.org"}, ctx)
    assert result.ok is False
    assert "git.example.org" in " ".join(result.errors + [str(result.value)])


# --- probing: one credential's failure is not the host's verdict --------------


def test_probe_reports_every_candidate_not_just_the_first(ctx):
    """The mistake this exists to prevent.

    A host-keyed credential helper returns ONE of the several credentials for a
    host. When that one 401s, the available conclusion is "this credential is
    dead" — and the conclusion actually drawn was "there is no working
    credential for this host", while six healthy ones sat in the same store.

    One sample, universal conclusion. Probing every candidate makes the correct
    answer as cheap as the wrong one.
    """
    calls = []

    def prober(name, meta):
        calls.append(name)
        return (False, "401") if name == "gl-git" else (True, "ok as bbooth")

    result = resolve_skill.run(
        {"host": "git.example.org", "probe": True, "_prober": prober}, ctx)

    assert set(calls) == {"gl-git", "gl-api"}
    by_name = {c["name"]: c for c in result.value["candidates"]}
    assert by_name["gl-git"]["alive"] is False
    assert by_name["gl-api"]["alive"] is True


def test_a_dead_default_with_a_live_sibling_is_reported_as_such(ctx):
    """The headline a caller needs, spelled out, so the wrong conclusion is not
    available to draw."""
    def prober(name, meta):
        return (name != "gl-git", "401" if name == "gl-git" else "ok")

    result = resolve_skill.run(
        {"host": "git.example.org", "probe": True, "_prober": prober}, ctx)
    assert result.value["any_alive"] is True
    assert result.value["git_helper_would_return"] == "gl-git"
    assert "gl-api" in result.value["alive"]
    assert "gl-git" in " ".join(result.errors)


def test_every_candidate_dead_says_so_plainly(ctx):
    """The one case where "no working credential for this host" IS the right
    conclusion. It has to be reachable, or the check is just optimism."""
    result = resolve_skill.run(
        {"host": "git.example.org", "probe": True,
         "_prober": lambda n, m: (False, "401")}, ctx)
    assert result.value["any_alive"] is False
    assert result.ok is False


def test_probing_never_returns_a_value(ctx):
    """Probing means USING the credential. The result must still be safe to
    print, log and paste."""
    import json

    result = resolve_skill.run(
        {"host": "git.example.org", "probe": True,
         "_prober": lambda n, m: (True, "ok")}, ctx)
    assert SECRET.decode() not in json.dumps(result.value)


def test_an_unprobeable_provider_says_so_rather_than_guessing(ctx):
    """`alive=None` is not `alive=False`. Reporting "unknown" as "dead" would
    recreate the original error from the other direction — declaring a healthy
    credential broken because nothing knew how to test it."""
    result = resolve_skill.run(
        {"host": "git.example.org", "probe": True,
         "_prober": lambda n, m: (None, "no probe for provider 'guided'")}, ctx)
    by_name = {c["name"]: c for c in result.value["candidates"]}
    assert by_name["gl-api"]["alive"] is None
    assert "no probe" in by_name["gl-api"]["probe_detail"]
    assert result.value["any_alive"] is None


def test_without_probe_nothing_is_contacted(ctx):
    """The default stays offline: listing candidates must not spend a network
    round trip per credential, or people stop running it."""
    called = []
    result = resolve_skill.run(
        {"host": "git.example.org", "_prober": lambda n, m: called.append(n)}, ctx)
    assert called == []
    assert all(c["alive"] is None for c in result.value["candidates"])
