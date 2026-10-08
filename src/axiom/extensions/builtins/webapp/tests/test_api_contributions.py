# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A verb joins /api/v1 without the API shell knowing the verb exists.

The unified surface only means something if the client talks to one base URL
with one auth posture and one versioning story. Axiom's mount registry cannot
express that alone: it refuses conflicting prefixes, so verbs cannot each mount
/api/v1/<verb> beside the one webapp holds.

The obvious alternative — webapp importing chat, rag and every domain pack — is
the dependency pointing the wrong way, and it means installing one verb drags in
the rest.

So a verb declares its slice in its own manifest and webapp discovers the
declaration. These tests pin the three properties that make that safe, each
chosen because the alternative fails quietly rather than loudly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pytest

from axiom.extensions.builtins.webapp.api import contributions as contrib
from axiom.extensions.contracts import ApiSurfaceDef


@dataclass
class _FakeExt:
    name: str
    api_surfaces: list = field(default_factory=list)
    enabled: bool = True


def _decl(subpath, entry="mod:register"):
    return ApiSurfaceDef(subpath=subpath, entry=entry)


def _contrib(subpath, entry="mod:register", extension="verb"):
    return contrib.ApiContribution(subpath=subpath, entry=entry, extension=extension)


# --- declaration hygiene ----------------------------------------------------

def test_a_subpath_must_be_a_slice_not_the_whole_surface():
    with pytest.raises(ValueError, match="slice, not the surface"):
        _contrib("/")


def test_a_subpath_must_be_rooted():
    with pytest.raises(ValueError, match="must start with"):
        _contrib("chat")


def test_an_entry_must_name_a_function():
    with pytest.raises(ValueError, match="module:function"):
        _contrib("/chat", entry="axiom.chat.api")


# --- collection -------------------------------------------------------------

def test_two_verbs_claiming_one_subpath_is_a_conflict():
    """Letting the second win would make the live API depend on import order."""
    a = _FakeExt("chat", [_decl("/chat")])
    b = _FakeExt("assistant", [_decl("/chat")])

    with pytest.raises(contrib.ApiSubpathConflictError, match="claimed by both"):
        contrib.collect_contributions([a, b])


def test_contributions_are_ordered_deterministically():
    """Discovery order follows the filesystem; the composed app must not."""
    exts = [
        (_FakeExt("search", [_decl("/search")])),
        (_FakeExt("chat", [_decl("/chat")])),
        (_FakeExt("library", [_decl("/library")])),
    ]
    got = [c.subpath for c in contrib.collect_contributions(exts)]
    assert got == sorted(got) == ["/chat", "/library", "/search"]


def test_a_disabled_extension_contributes_nothing():
    ext = (_FakeExt("chat", [_decl("/chat")]))
    ext.enabled = False
    assert contrib.collect_contributions([ext]) == []


# --- application ------------------------------------------------------------

def test_a_contribution_registers_its_routes(monkeypatch):
    from fastapi import APIRouter

    router = APIRouter(prefix="/api/v1")
    calls = []

    def register(r, *, subpath):
        calls.append(subpath)

        @r.get(subpath + "/ping")
        def ping():
            return {"ok": True}

    monkeypatch.setattr(
        contrib, "import_module", lambda _: None, raising=False
    )
    import sys
    import types

    mod = types.ModuleType("fake_verb_mod")
    mod.register = register
    sys.modules["fake_verb_mod"] = mod

    landed = contrib.apply_contributions(
        router, [_contrib("/chat", entry="fake_verb_mod:register", extension="chat")]
    )
    assert landed == ["/chat"]
    assert calls == ["/chat"]
    assert "/api/v1/chat/ping" in {r.path for r in router.routes}


def test_a_broken_verb_costs_its_own_routes_not_the_api(caplog):
    """One verb failing must not take the surface down — but must be loud.

    A route that silently fails to register is a 404 that looks like a client
    bug and gets debugged in the wrong place entirely.
    """
    from fastapi import APIRouter

    router = APIRouter(prefix="/api/v1")
    with caplog.at_level(logging.ERROR, logger=contrib.__name__):
        landed = contrib.apply_contributions(
            router,
            [
                _contrib("/broken", entry="no.such.module:register",
                         extension="broken-verb"),
                _contrib("/fine", entry="fake_verb_mod:register", extension="fine"),
            ],
        )

    assert landed == ["/fine"], "a healthy verb was lost to a broken sibling"
    blob = " ".join(r.message for r in caplog.records)
    assert "broken-verb" in blob and "404" in blob, (
        f"the failure was not explained: {[r.message for r in caplog.records]}"
    )


# --- the whole chain, from a real manifest ----------------------------------

def test_a_manifest_declaration_becomes_a_live_route(tmp_path, monkeypatch):
    """Discovery -> manifest parse -> collect -> apply, with nothing faked.

    The unit tests above exercise each link. This proves the chain, because a
    registry that works only against hand-built objects is a registry that has
    never met a manifest — and the parsing half lives in contracts.py, far from
    the code that consumes it.
    """
    import sys
    import types

    from fastapi import APIRouter

    from axiom.extensions.discovery import discover_extensions

    # A verb that registers one route, importable by name.
    verb = types.ModuleType("proof_verb")

    def register_routes(router, *, subpath):
        @router.get(subpath + "/status")
        def status():
            return {"verb": "proof"}

    verb.register_routes = register_routes
    sys.modules["proof_verb"] = verb

    ext_dir = tmp_path / "proof"
    ext_dir.mkdir()
    (ext_dir / "axiom-extension.toml").write_text(
        '\n'.join([
            '[extension]',
            'name = "proof"',
            'version = "0.1.0"',
            'description = "a verb that contributes an api surface"',
            'aeos_version = "0.1.0"',
            '',
            '[[extension.provides]]',
            'kind = "api"',
            'subpath = "/proof"',
            'entry = "proof_verb:register_routes"',
            'description = "the proof surface"',
        ]),
        encoding="utf-8",
    )

    found = [e for e in discover_extensions(tmp_path) if e.name == "proof"]
    assert found, "the manifest was not discovered at all"
    assert found[0].api_surfaces, (
        "kind = 'api' was discovered but not parsed into api_surfaces — the "
        "contracts.py half of the chain is broken"
    )

    router = APIRouter(prefix="/api/v1")
    landed = contrib.apply_contributions(
        router, contrib.collect_contributions(found)
    )
    assert landed == ["/proof"]
    assert "/api/v1/proof/status" in {r.path for r in router.routes}
