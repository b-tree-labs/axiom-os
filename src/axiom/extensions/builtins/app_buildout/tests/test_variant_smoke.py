# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VAR-A4: each variant's smoke on the web app's paths.

For each variant, the served ``/api/v1`` surface (``webapp.api.routers``, with
every contributed verb, and the Library's nested mount) is built in that
variant's environment, exactly as ``axi app run`` starts it, and the six
surfaces the shell depends on are asked once: the rail's flags, search, chat,
Chart, Library, and health. A session from the variant's own realm is
answered; the same requests under another variant's session are refused.

CI runs this module in the ``Variant Smoke`` job, only when a web-app path
changed (``.github/workflows/ci.yml``). Databases are private in-memory SQLite
through the extensions' own test seams; nothing touches the dev database.
"""

from __future__ import annotations

import contextlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.infra import variants as v
from axiom.webauth import session as s
from axiom.webauth.users import User

pytestmark = pytest.mark.acceptance("VAR-A4")


def _memory_engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False},
                         poolclass=StaticPool, future=True)


@pytest.fixture
def seams(monkeypatch, tmp_path):
    from axiom.extensions.builtins.chat import api as chat_api
    from axiom.extensions.builtins.chat.db_models import Base as ChatBase
    from axiom.extensions.builtins.flags import registry as flag_registry
    from axiom.extensions.builtins.flags import store as flag_store
    from axiom.extensions.builtins.flags.db_models import Base as FlagBase
    from axiom.extensions.builtins.flags.registry import FlagDef
    from axiom.webauth.keys import reset_key_store_for_tests

    reset_key_store_for_tests()
    # The rail's items, pinned so the smoke does not depend on which manifests
    # this machine discovers; each variant sets one through its own layer.
    flag_registry.set_registry(
        [FlagDef(key=k, description=k, default="on", owner="smoke") for k in RAIL])
    cfg = tmp_path / "variants.toml"
    cfg.write_text("".join(f'[variant.{n}.flags]\nlog = "{state}"\n'
                           for n, state in VARIANT_LOG.items()))
    monkeypatch.setenv(v.CONFIG_ENV, str(cfg))
    monkeypatch.setenv("AXIOM_LIBRARY_BRONZE_ROOT", str(tmp_path / "bronze"))
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)

    flags_engine = _memory_engine()
    FlagBase.metadata.create_all(flags_engine)
    factory = sessionmaker(bind=flags_engine, expire_on_commit=False)

    @contextlib.contextmanager
    def provider():
        sess = factory()
        try:
            yield sess
        finally:
            sess.close()

    flag_store.set_provider(provider)
    chat_engine = _memory_engine()
    ChatBase.metadata.create_all(chat_engine)
    chat_api.set_test_engine(chat_engine)
    yield
    chat_api.set_test_engine(None)
    flag_store.reset_provider()
    flag_registry.set_registry(None)
    flags_engine.dispose()
    chat_engine.dispose()


def _serve_as(monkeypatch, name: str) -> TestClient:
    """The variant's served API, in the environment ``axi app run`` gives it."""
    for key in (s.REALM_ENV, s.COOKIE_ENV, s.LEGACY_ENV):
        monkeypatch.delenv(key, raising=False)
    for key, value in v.get_variant(name).child_env().items():
        monkeypatch.setenv(key, value)
    from axiom.extensions.builtins.library.mount import mount_spec as library_mount
    from axiom.extensions.builtins.webapp.api.routers import build_api_router

    app = FastAPI()
    app.include_router(build_api_router())
    app.include_router(library_mount().router)  # its routes carry their full path
    return TestClient(app)


def _cookie_for(monkeypatch, name: str) -> tuple[str, str]:
    for key, value in v.get_variant(name).child_env().items():
        monkeypatch.setenv(key, value)
    user = User(user_id="@ada:example", email="ada@example.org", name="Ada", roles=("viewer",))
    return v.get_variant(name).cookie, s.issue_session_token(user)


#: The standard rail's items, and the state each variant's own layer gives Log.
RAIL = ("chat", "site", "model", "chart", "log", "library", "team", "history", "search")
VARIANT_LOG = {"axiom": "off", "product": "soon", "site": "soon"}

#: (surface, method, path, body): the shell's six dependencies.
SURFACES = [
    ("health", "get", "/api/v1/health", None),
    ("rail flags", "get", "/api/v1/flags", None),
    ("search", "get", "/api/v1/search/suggest?q=smoke", None),
    ("chat", "get", "/api/v1/chat/conversations", None),
    ("chart", "post", "/api/v1/charts/suggest", {"result": {"rows": [{"g": "a", "n": 3}, {"g": "b", "n": 5}]},
                                               "question": "which is most"}),
    ("library", "get", "/api/v1/library/taxonomies", None),
]


@pytest.mark.parametrize("name", v.NAMES)
def test_var_a4_each_variant_answers_its_shell_surfaces(monkeypatch, seams, name):
    cookie, token = _cookie_for(monkeypatch, name)
    client = _serve_as(monkeypatch, name)
    client.cookies.set(cookie, token)
    failures = []
    for surface, method, path, body in SURFACES:
        r = getattr(client, method)(path, **({"json": body} if body is not None else {}))
        if r.status_code >= 400:
            failures.append(f"{surface} {path}: {r.status_code} {r.text[:200]}")
    assert not failures, "\n".join(failures)
    flags = client.get("/api/v1/flags").json()
    assert flags["variant"] == name
    assert set(RAIL) <= set(flags["flags"])
    assert (flags["flags"]["log"], flags["sources"]["log"]) == (VARIANT_LOG[name], "variant")


@pytest.mark.parametrize("name,other", [(a, b) for a in v.NAMES for b in v.NAMES if a != b])
def test_var_a4_a_variant_refuses_another_variants_session(monkeypatch, seams, name, other):
    cookie, token = _cookie_for(monkeypatch, other)
    client = _serve_as(monkeypatch, name)
    client.cookies.set(cookie, token)
    client.cookies.set(v.get_variant(name).cookie, token)  # replayed under this variant's name
    assert client.get("/api/v1/flags").status_code == 401
