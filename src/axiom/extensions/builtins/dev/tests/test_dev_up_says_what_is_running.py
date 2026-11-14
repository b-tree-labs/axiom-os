# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`dev up` and `dev status` say what is running (#1716, first slice).

A contributor ran `dev up` from a source clone with a site named in his
environment and could not tell what he was looking at: the bare platform, the
bare product, or a site. Nothing said which code ran, which platform it ran
on, that the site's look was not applied, where the data came from, which
database it used, or how to sign in. One block now says all of it, the same
on a fresh start, on "already running", and in `dev status`, and as JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from axiom.extensions.builtins.dev import node, running
from axiom.extensions.builtins.dev.cli import _render

# -- fakes -------------------------------------------------------------------


@dataclass
class FakeMode:
    mode: str = "local"
    site: str = ""
    face_url: str = ""

    @property
    def bound(self) -> bool:
        return self.mode == "bound"


class FakeDist:
    """An installed distribution, with or without a ``direct_url.json``."""

    def __init__(self, version: str, direct_url: dict | None = None) -> None:
        self.version = version
        self._direct = direct_url

    def read_text(self, name: str) -> str | None:
        if name == "direct_url.json" and self._direct is not None:
            return json.dumps(self._direct)
        return None


def _clone(tmp_path: Path, name: str, version: str) -> Path:
    clone = tmp_path / name
    (clone / "src" / "axiom").mkdir(parents=True)
    (clone / "src" / "axiom" / "__init__.py").write_text("", encoding="utf-8")
    (clone / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8"
    )
    return clone


# -- Platform ----------------------------------------------------------------


def test_an_editable_platform_names_its_clone(tmp_path) -> None:
    clone = _clone(tmp_path, "platform-clone", "9.9.1")
    dist = FakeDist("9.9.0", {"url": clone.as_uri(), "dir_info": {"editable": True}})
    p = running.platform(roots=[], dist=dist)
    assert p == {"name": "Axiom", "version": "9.9.1", "source": "editable", "path": str(clone)}
    assert running.platform_line(p) == f"Platform: Axiom 9.9.1 from {clone}"


def test_an_installed_platform_says_installed_package(tmp_path) -> None:
    p = running.platform(roots=[], dist=FakeDist("9.9.0"))
    assert p == {"name": "Axiom", "version": "9.9.0", "source": "installed", "path": None}
    assert running.platform_line(p) == "Platform: Axiom 9.9.0 from installed package"


def test_a_platform_on_the_nodes_source_path_wins_over_the_installed_one(tmp_path) -> None:
    # The node runs its source roots ahead of the installed package, so a
    # platform found there is the one actually running.
    clone = _clone(tmp_path, "with-clone", "9.9.2")
    p = running.platform(roots=[clone / "src"], dist=FakeDist("9.9.0"))
    assert p["path"] == str(clone)
    assert p["version"] == "9.9.2"


# -- Site look ---------------------------------------------------------------


def test_no_site_named_says_sites_are_not_applied_locally_yet() -> None:
    look = running.site_look(env={}, mode=FakeMode())
    assert look == {"applied": False, "requested": None, "issue": "#1716"}
    assert running.site_look_line(look) == (
        "Site look: none (sites are not applied locally yet, #1716)"
    )


def test_a_site_in_the_environment_is_named_as_not_applied() -> None:
    # A set variable must never silently look like it worked.
    look = running.site_look(env={"AXIOM_SITE": "site-a"}, mode=FakeMode())
    assert look["requested"] == "site-a"
    assert running.site_look_line(look) == (
        "Site look: none: site-a is not applied locally yet (#1716)"
    )


def test_a_bound_site_is_named_as_not_applied_when_no_variable_is_set() -> None:
    mode = FakeMode("bound", "site-b", "https://face.example.org")
    look = running.site_look(env={}, mode=mode)
    assert look["requested"] == "site-b"


# -- Data --------------------------------------------------------------------


def test_data_is_local_when_unbound() -> None:
    data = running.data(FakeMode())
    assert data == {"bound": False, "site": None, "host": None}
    assert running.data_line(data) == "Data: local"


def test_data_names_the_bound_site_and_only_its_host() -> None:
    mode = FakeMode("bound", "site-b", "https://face.example.org:8443/some/path?k=secret")
    data = running.data(mode)
    assert data == {"bound": True, "site": "site-b", "host": "face.example.org"}
    assert running.data_line(data) == "Data: bound to site-b at face.example.org"


# -- Sign in -----------------------------------------------------------------


def test_sign_in_by_password_names_the_email() -> None:
    s = running.sign_in(email="a@example.org", sso_host=None)
    assert s == {"method": "password", "host": None, "email": "a@example.org", "source": None}
    assert running.sign_in_line(s) == "Sign in: password as a@example.org"


def test_sign_in_by_sso_names_the_issuer_host() -> None:
    s = running.sign_in(email="a@example.org", sso_host="login.example.org")
    assert s["method"] == "sso"
    assert running.sign_in_line(s) == (
        "Sign in: SSO via login.example.org, or with a password as a@example.org"
    )


def test_an_unreachable_provider_is_said_plainly_and_sso_is_not_promised() -> None:
    """#1842: the line says what is enforced, not what was asked for."""
    s = running.sign_in(email="a@example.org", sso_host="login.example.org", sso_reachable=False)
    assert s["reachable"] is False
    line = running.sign_in_line(s)
    assert "SSO via" not in line
    assert line == (
        "Sign in: login.example.org cannot be reached, so SSO is refused until it "
        "answers; a password works as a@example.org"
    )


def test_a_reachable_provider_carries_no_reachable_key() -> None:
    """Negative control: the shape the web shell reads is unchanged when all is well."""
    assert "reachable" not in running.sign_in(email="a@example.org", sso_host="login.example.org")


# -- the whole block, and its JSON -------------------------------------------


def _fixture(tmp_path: Path) -> dict:
    return running.describe(
        product_name="Acme OS",
        product_version="1.2.3",
        checkout=tmp_path / "acme-checkout",
        platform={"name": "Axiom", "version": "9.9.0", "source": "installed", "path": None},
        env={"AXIOM_SITE": "site-a"},
        mode=FakeMode("bound", "site-a", "https://face.example.org"),
        database="localhost:5432/acme_dev",
        email="a@example.org",
        sso_host="login.example.org",
    )


def test_the_full_block(tmp_path) -> None:
    out = "\n".join(running.render(_fixture(tmp_path)))
    assert out == (
        f"Running: Acme OS from {tmp_path / 'acme-checkout'} (1.2.3)\n"
        "  Platform: Axiom 9.9.0 from installed package\n"
        "  Site look: none: site-a is not applied locally yet (#1716)\n"
        "  Data: bound to site-a at face.example.org\n"
        "  Library: site-a's, relayed to face.example.org\n"
        "  Database: localhost:5432/acme_dev\n"
        "  Sign in: SSO via login.example.org, or with a password as a@example.org"
    )


def test_the_json_shape(tmp_path) -> None:
    d = _fixture(tmp_path)
    assert json.loads(json.dumps(d)) == d  # plain JSON, nothing else
    assert set(d) == {"product", "platform", "site_look", "data", "library", "database", "sign_in"}
    assert set(d["library"]) == {"local", "root", "site", "host"}
    assert d["product"] == {
        "name": "Acme OS",
        "version": "1.2.3",
        "checkout": str(tmp_path / "acme-checkout"),
    }
    assert set(d["platform"]) == {"name", "version", "source", "path"}
    assert set(d["site_look"]) == {"applied", "requested", "issue"}
    assert set(d["data"]) == {"bound", "site", "host"}
    assert set(d["sign_in"]) == {"method", "host", "email", "source"}


def test_the_database_line_never_carries_a_user_or_password(tmp_path) -> None:
    d = running.describe(
        product_name="Acme OS",
        product_version="1.2.3",
        checkout=tmp_path,
        platform={"name": "Axiom", "version": "9.9.0", "source": "installed", "path": None},
        env={},
        mode=FakeMode(),
        database=node.describe_database("postgresql://who:hunter2@db.example.org:6543/x"),
        email="a@example.org",
        sso_host=None,
    )
    out = "\n".join(running.render(d))
    assert "Database: db.example.org:6543/x" in out
    assert "hunter2" not in out and "who" not in out


def test_the_product_is_the_branded_one_never_a_hard_coded_name(tmp_path, monkeypatch) -> None:
    from axiom.infra import branding

    branding.reset()
    try:
        branding.register(branding.BrandingConfig(cli_name="acme", product_name="Acme OS"))
        name, _version = running.product(tmp_path)
        assert name == "Acme OS"
    finally:
        branding.reset()
    name, _ = running.product(tmp_path)
    assert name == "Axiom"


def test_the_product_version_is_the_checkouts_own(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "acme"\nversion = "4.5.6"\n', encoding="utf-8"
    )
    assert running.product(tmp_path)[1] == "4.5.6"


# -- through dev up and dev status ----------------------------------------------


def test_a_fresh_start_prints_the_block_ahead_of_the_url_and_does_not_repeat_it(
    harness, monkeypatch
) -> None:
    from axiom.extensions.builtins.dev.skills import up

    monkeypatch.setenv(node.DB_URL_VAR, "postgresql://u:p@localhost:5432/lane1")
    monkeypatch.delenv("AXIOM_SITE", raising=False)
    result = up.run({"email": "a@example.org"}, harness["ctx"])
    assert result.ok, result.errors
    r = result.value["running"]
    assert r["sign_in"] == {
        "method": "password",
        "host": None,
        "email": "a@example.org",
        "source": None,
    }
    # The lane gets its own database on that server (worktree-aware schema, local-run rule 15).
    assert r["database"] == "localhost:5432/axiom_dev_lane1"
    out = _render("up", result)
    assert out.lstrip().startswith("Running: ")
    assert out.count("Database:") == 1
    assert out.count("Sign in") == 1
    assert out.index("Running:") < out.index("http://127.0.0.1:8899/app/")
    assert "dev down" in out


def _lane(root) -> dict:
    """A managed lane, as `axi lane claim` records one: its ports and its own database."""
    return {
        "name": "lane1",
        "front": 8898,
        "api": 8899,
        "database": "axiom_dev_lane1",
        "dsn_var": "AXIOM_DB_URL",
        "isolated": True,
        "root": str(root),
    }


def _already_running(tmp_path, monkeypatch, record: dict):
    from axiom.extensions.builtins.dev.skills import up

    monkeypatch.setattr(up, "resolve_lane", lambda params: (_lane(tmp_path), []))
    monkeypatch.setattr(node, "read_record", lambda home: dict(record))
    monkeypatch.setattr(node, "alive", lambda pid: True)
    monkeypatch.setattr(node, "answers", lambda url: True)

    class Ctx:
        state_dir = tmp_path

    return up, Ctx()


def _record(tmp_path: Path, email: str, site: str | None = None) -> dict:
    described = running.describe(
        product_name="Acme OS",
        product_version="1.2.3",
        checkout=tmp_path,
        platform={"name": "Axiom", "version": "9.9.0", "source": "installed", "path": None},
        env={"AXIOM_SITE": site} if site else {},
        mode=FakeMode(),
        database="localhost:5432/lane1",
        email=email,
        sso_host=None,
    )
    return {
        "pid": 1,
        "url": "http://127.0.0.1:8830/app/",
        "root": str(tmp_path),
        "email": email,
        "running": described,
    }


def test_already_running_as_another_email_says_so_instead_of_echoing_it(
    tmp_path, monkeypatch
) -> None:
    up, ctx = _already_running(tmp_path, monkeypatch, _record(tmp_path, "a@example.org"))
    result = up.run({"email": "b@example.org"}, ctx)
    assert result.ok
    assert result.value["email"] == "a@example.org"
    out = _render("up", result)
    assert "Running as a@example.org; add --restart to switch to b@example.org" in out
    assert "Sign in: password as a@example.org" in out
    assert "as b@example.org" not in out.replace("switch to b@example.org", "")


def test_already_running_as_the_same_email_says_nothing_extra(tmp_path, monkeypatch) -> None:
    up, ctx = _already_running(tmp_path, monkeypatch, _record(tmp_path, "a@example.org"))
    out = _render("up", up.run({"email": "a@example.org"}, ctx))
    assert "--restart to switch" not in out
    assert out.lstrip().startswith("already running")
    assert "Running: Acme OS" in out


def test_already_running_with_another_site_says_so(tmp_path, monkeypatch) -> None:
    up, ctx = _already_running(tmp_path, monkeypatch, _record(tmp_path, "a@example.org"))
    monkeypatch.setenv("AXIOM_SITE", "site-c")
    out = _render("up", up.run({}, ctx))
    assert "Running with no site; add --restart to switch to site-c" in out


def test_dev_status_prints_the_same_block(tmp_path, monkeypatch) -> None:
    from axiom.extensions.builtins.dev.skills import status

    monkeypatch.setattr(status, "resolve_lane", lambda params: (_lane(tmp_path), []))
    monkeypatch.setattr(node, "read_record", lambda home: _record(tmp_path, "a@example.org"))
    monkeypatch.setattr(node, "alive", lambda pid: True)
    monkeypatch.setattr(node, "answers", lambda url: True)

    class Ctx:
        state_dir = tmp_path

    result = status.run({}, Ctx())
    assert result.value["running"]["product"]["name"] == "Acme OS"
    out = _render("status", result)
    assert out.splitlines()[0] == "up: http://127.0.0.1:8830/app/"
    assert "Running: Acme OS" in out
    assert "Sign in: password as a@example.org" in out


@pytest.mark.parametrize("state_record", [None])
def test_dev_status_when_down_prints_no_block(tmp_path, monkeypatch, state_record) -> None:
    from axiom.extensions.builtins.dev.skills import status

    monkeypatch.setattr(status, "resolve_lane", lambda params: (_lane(tmp_path), []))
    monkeypatch.setattr(node, "read_record", lambda home: state_record)

    class Ctx:
        state_dir = tmp_path

    out = _render("status", status.run({}, Ctx()))
    assert out == "down"
