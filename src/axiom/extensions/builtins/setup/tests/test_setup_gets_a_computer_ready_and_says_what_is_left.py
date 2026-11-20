# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One command gets a computer ready, asks before each change, and ends with a checklist.

The colleague guide this replaces had a person export two variables, start a vault,
edit a shell profile, open a page, copy a key, paste it into another command, bind,
and register their AI app: nine steps, any of which could be skipped or done in the
wrong terminal. Live onboarding on 2026-10-26 failed at nine distinct points.

``setup`` does those steps itself, each after a yes, and ``doctor`` runs the same
checks without changing anything. Every test here runs against a temporary home
and a temporary state directory; none of them touches the real ``~/.axi``, a real
shell profile, a real vault or a real AI app's config.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from axiom.extensions.builtins.setup.environment import SetupEnv, resolve_env
from axiom.extensions.builtins.setup.runner import render_checklist, run_steps
from axiom.extensions.builtins.setup.steps import (
    KeyStep,
    Line,
    McpStep,
    NodeStep,
    ShellStep,
    Step,
    UpgradeStep,
    VaultStep,
)

TOKEN_VALUE = "hvs.TEST-ONLY-NOT-A-REAL-TOKEN"


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AXI_STATE_DIR", str(home / ".axi"))
    for var in ("AXIOM_FOREIGN_SECRETS_BACKEND", "AXIOM_OPENBAO_URL", "AXIOM_OPENBAO_TOKEN",
                "AXIOM_NODE_URL", "AXI_SETUP_RESUMED"):
        monkeypatch.delenv(var, raising=False)
    return home


def _env(home: Path, **kw) -> SetupEnv:
    said: list[str] = []
    env = SetupEnv(home=home, state_dir=home / ".axi", system="darwin", cli="axi",
                   product="Axiom", say=said.append, **kw)
    env.said = said  # type: ignore[attr-defined]
    return env


class Recording(Step):
    """A step whose state is a flag: red until applied."""

    def __init__(self, name="thing", needs=(), plan="do the thing"):
        self.name, self.title, self.needs, self._plan = name, name.title(), needs, plan
        self.done, self.applied = False, 0

    def check(self, env):
        return self.ok("done") if self.done else self.red("not done", f"run `{env.cli} setup`")

    def plan(self, env):
        return self._plan

    def apply(self, env):
        self.applied += 1
        self.done = True
        return self.check(env)


# --------------------------------------------------------------------------- the walk


def test_nothing_is_changed_without_a_yes(temp_home):
    step = Recording()
    lines = run_steps(_env(temp_home), [step], ask=lambda _q: False)["lines"]
    assert step.applied == 0
    assert not lines[0].ok and "you said no" in lines[0].status


def test_without_a_terminal_and_without_yes_nothing_is_changed_and_it_says_how(temp_home):
    step = Recording()
    line = run_steps(_env(temp_home), [step], ask=None)["lines"][0]
    assert step.applied == 0
    assert "--yes" in line.fix


def test_a_yes_applies_the_step_and_the_line_reports_what_was_observed(temp_home):
    step = Recording()
    asked: list[str] = []
    line = run_steps(_env(temp_home), [step], ask=lambda q: asked.append(q) or True)["lines"][0]
    assert step.applied == 1 and line.ok
    assert asked == ["Thing: do the thing?"], "the question must say what will change"


def test_a_dry_run_says_what_would_change_and_changes_nothing(temp_home):
    step = Recording()
    env = _env(temp_home)
    line = run_steps(env, [step], mode="plan", ask=lambda _q: True)["lines"][0]
    assert step.applied == 0
    assert "would do the thing" in "\n".join(env.said)
    assert "would do the thing" in line.fix


def test_running_again_skips_what_is_done(temp_home):
    step = Recording()
    env = _env(temp_home)
    run_steps(env, [step], ask=lambda _q: True)
    run_steps(env, [step], ask=lambda _q: pytest.fail("asked about a step that is already done"))
    assert step.applied == 1


def test_a_step_waits_for_the_step_it_needs_and_says_which(temp_home):
    first, second = Recording("vault"), Recording("key", needs=("vault",))
    lines = run_steps(_env(temp_home), [first, second], ask=lambda _q: False)["lines"]
    assert second.applied == 0
    assert lines[1].status == "waits for: Vault"
    assert lines[1].fix == "fix the Vault line first"


def test_a_check_that_raises_costs_only_its_own_line(temp_home):
    class Broken(Step):
        name, title = "broken", "Broken"

        def check(self, env):
            raise RuntimeError("boom")

    after = Recording("after")
    lines = run_steps(_env(temp_home), [Broken(), after], ask=lambda _q: True)["lines"]
    assert not lines[0].ok and "boom" in lines[0].status
    assert lines[1].ok, "one broken step stopped the rest"


def test_the_checklist_is_green_for_what_works_and_gives_one_remedy_per_red_line():
    text = render_checklist(
        [Line("a", "Vault", True, "OpenBao at x"),
         Line("b", "Node", False, "did not answer", "connect your VPN"),
         Line("c", "AI apps", False, "none found", skipped=True)],
        cli="demo",
    )
    assert "✓ Vault" in text and "✗ Node" in text and "· AI apps" in text
    assert text.count("→") == 1, "a skipped line is not something to fix"
    assert "→ connect your VPN" in text
    assert "1 thing to fix" in text and "`demo setup` again" in text


def test_the_checks_change_nothing(temp_home):
    """``doctor`` runs these. A check that wrote a file would make doctor a setup."""
    from axiom.extensions.builtins.setup.steps import all_steps

    def files():
        # Files, not folders: the platform's path helpers create the empty state
        # folder on lookup, which writes nothing.
        return sorted(p.relative_to(temp_home) for p in temp_home.rglob("*") if p.is_file())

    before = files()
    env = _env(temp_home, node="http://127.0.0.1:9", seams={
        "installer": "checkout", "detect_tools": lambda: {},
        "run": lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="")})
    run_steps(env, all_steps(), mode="check")
    assert files() == before


def test_doctor_shows_the_setup_checks(temp_home):
    from axiom.axiom_cli import _setup_checks

    rows = _setup_checks()
    names = [r["name"] for r in rows]
    for title in ("Vault", "Node", "Signed in", "AI apps"):
        assert f"Setup: {title}" in names
    for row in rows:
        assert row["ok"] or row["fix"], f"a red doctor row with no remedy: {row}"


# --------------------------------------------------------------------------- where the node comes from


def test_the_node_typed_wins_then_the_environment_then_the_last_run(temp_home, monkeypatch):
    from axiom.extensions.builtins.setup.environment import save_state

    save_state(temp_home / ".axi", node="https://last.example.org", site="", lines=[])
    assert resolve_env({}).node == "https://last.example.org"
    monkeypatch.setenv("AXIOM_NODE_URL", "https://env.example.org/")
    assert resolve_env({}).node == "https://env.example.org"
    assert resolve_env({"node": "https://typed.example.org"}).node == "https://typed.example.org"


def test_a_product_supplies_its_default_node_without_the_platform_knowing_it(temp_home):
    from axiom.infra import branding

    brand = branding.BrandingConfig(cli_name="demo", product_name="Demo", package_name="demo",
                                    setup_defaults_fn=lambda: {"node": "https://site.example.org",
                                                               "site": "example"})
    branding.register(brand)
    try:
        env = resolve_env({})
        assert (env.node, env.site, env.cli) == ("https://site.example.org", "example", "demo")
    finally:
        branding.reset()


def test_a_product_adds_its_own_step_after_sign_in(temp_home):
    from axiom.extensions.builtins.setup.steps import all_steps
    from axiom.infra import branding

    class Bind(Step):
        name, title, needs = "bind", "Bound", ("key",)

        def check(self, env):
            return self.ok("bound")

    branding.register(branding.BrandingConfig(cli_name="demo", product_name="Demo",
                                              package_name="demo", setup_steps_fn=lambda: [Bind()]))
    try:
        names = [s.name for s in all_steps()]
    finally:
        branding.reset()
    assert names.index("bind") == names.index("key") + 1
    assert names[-1] == "mcp"


def test_a_product_step_hook_that_raises_costs_only_its_steps(temp_home):
    from axiom.extensions.builtins.setup.steps import all_steps, builtin_steps
    from axiom.infra import branding

    def boom():
        raise RuntimeError("brand is broken")

    branding.register(branding.BrandingConfig(cli_name="demo", product_name="Demo",
                                              package_name="demo", setup_steps_fn=boom))
    try:
        assert [s.name for s in all_steps()] == [s.name for s in builtin_steps()]
    finally:
        branding.reset()


# --------------------------------------------------------------------------- upgrade


def _upgrade_env(home, ran):
    from axiom.extensions.builtins.update.installer import InstallPlan

    return _env(home, seams={
        "installer": "uv tool",
        "versions": lambda: ("1.0.0", "1.1.0", True),
        "upgrade_plan": InstallPlan("uv-tool", ["uv", "tool", "upgrade", "demo"], "uv tool upgrade demo"),
        "run": lambda cmd, **k: ran.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    })


def test_an_old_install_is_upgraded_with_the_tool_that_installed_it_and_setup_resumes(temp_home):
    ran: list = []
    env = _upgrade_env(temp_home, ran)
    after = Recording("after")
    result = run_steps(env, [UpgradeStep(), after], ask=lambda _q: True)
    assert ran == [["uv", "tool", "upgrade", "demo"]]
    assert result["resume"], "the rest of setup must continue on the new code"
    assert after.applied == 0, "steps after an upgrade ran on the old code"


def test_an_upgrade_is_not_retried_in_the_resumed_run(temp_home, monkeypatch):
    monkeypatch.setenv("AXI_SETUP_RESUMED", "1")
    ran: list = []
    line = run_steps(_upgrade_env(temp_home, ran), [UpgradeStep()], ask=lambda _q: True)["lines"][0]
    assert ran == [] and not line.ok
    assert "uv tool upgrade demo" in line.fix


def test_a_source_checkout_is_not_upgraded(temp_home):
    line = UpgradeStep().check(_env(temp_home, seams={"installer": "checkout"}))
    assert line.skipped and line.green


# --------------------------------------------------------------------------- vault


class FakeVault:
    """The OpenBao HTTP API as ``vault_setup.Vault`` speaks it (see that module's tests)."""

    def __init__(self, addr):
        self.addr, self.initialized, self.sealed = addr, False, True

    def reachable(self):
        return True

    def call(self, method, path, body=None, token=None):
        if path == "/v1/sys/init" and method == "GET":
            return {"initialized": self.initialized}
        if path == "/v1/sys/init":
            self.initialized = True
            return {"keys": ["FAKE-UNSEAL-KEY"], "root_token": "FAKE-ROOT"}
        if path == "/v1/sys/seal-status":
            return {"sealed": self.sealed}
        if path == "/v1/sys/unseal":
            self.sealed = False
        if path == "/v1/auth/token/create":
            return {"auth": {"client_token": TOKEN_VALUE}}
        return {}


def _docker(cmd, **_kw):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_the_vault_is_started_in_docker_and_then_found_running(temp_home):
    vault = FakeVault("")
    env = _env(temp_home, seams={"run": _docker, "which": lambda n: f"/usr/bin/{n}",
                                 "vault_factory": lambda a: vault, "sleep": lambda s: None})
    step = VaultStep()
    assert not step.check(env).ok
    assert "Docker" in step.plan(env)
    line = run_steps(env, [step], ask=lambda _q: True)["lines"][0]
    assert line.ok, line
    assert "unsealed" in line.status
    assert TOKEN_VALUE not in "\n".join(env.said)
    assert (temp_home / ".axi" / "config" / "secrets.toml").exists(), "apps would not find it"


def test_a_sealed_vault_is_red_and_setup_unseals_it(temp_home):
    vault = FakeVault("")
    env = _env(temp_home, seams={"run": _docker, "which": lambda n: f"/usr/bin/{n}",
                                 "vault_factory": lambda a: vault, "sleep": lambda s: None})
    run_steps(env, [VaultStep()], ask=lambda _q: True)
    vault.sealed = True  # a restart: OpenBao always starts sealed
    line = VaultStep().check(env)
    assert not line.ok and "sealed" in line.status and "setup` again" in line.fix
    assert run_steps(env, [VaultStep()], ask=lambda _q: True)["lines"][0].ok


def test_with_neither_docker_nor_openbao_the_remedy_says_what_to_install(temp_home):
    env = _env(temp_home, seams={"which": lambda n: None, "run": _docker})
    line = VaultStep().check(env)
    assert not line.ok
    assert "brew install openbao" in line.fix and "Docker" in line.fix
    assert VaultStep().plan(env) is None


# --------------------------------------------------------------------------- shell


def _openbao_configured(home: Path):
    from axiom.extensions.builtins.secrets import vault_settings

    token = home / ".axi" / "openbao" / "axi-token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(TOKEN_VALUE)
    token.chmod(0o600)
    vault_settings.write(backend="openbao", url="http://127.0.0.1:8200",
                         token_file=str(token), mount="kv")
    return token


def _shell_env(home):
    return _env(home, seams={"environ": {"SHELL": "/bin/zsh"}, "which": lambda n: None,
                             "run": lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="")})


def test_the_shell_gets_a_marked_block_in_its_own_start_up_file_after_a_yes(temp_home):
    _openbao_configured(temp_home)
    rc = temp_home / ".zshrc"
    rc.write_text("export MINE=1\n")
    env = _shell_env(temp_home)
    assert not ShellStep().check(env).ok
    line = run_steps(env, [ShellStep()], ask=lambda _q: True)["lines"][0]
    assert line.ok, line
    text = rc.read_text()
    assert text.startswith("export MINE=1\n"), "the person's own lines were disturbed"
    assert "# >>> " in text and "# <<< " in text
    assert TOKEN_VALUE not in text, "the token itself was written into a profile"
    assert list(temp_home.glob(".zshrc.vault-setup-bak-*")), "no backup was made first"


def test_wiring_the_shell_twice_changes_nothing(temp_home):
    _openbao_configured(temp_home)
    env = _shell_env(temp_home)
    run_steps(env, [ShellStep()], ask=lambda _q: True)
    once = (temp_home / ".zshrc").read_text()
    run_steps(env, [ShellStep()], ask=lambda _q: pytest.fail("asked again"))
    assert (temp_home / ".zshrc").read_text() == once


def test_a_shell_is_not_touched_when_there_is_no_local_vault(temp_home):
    line = ShellStep().check(_shell_env(temp_home))
    assert line.skipped and line.green
    assert not (temp_home / ".zshrc").exists()


# --------------------------------------------------------------------------- node


@pytest.fixture
def tiny_node():
    """A real HTTP server on 127.0.0.1 that answers like a gate's Keys page."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def do_GET(self):  # noqa: N802
            self.send_response(303)
            self.send_header("Location", "/gate/login")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_a_node_that_answers_is_green(temp_home, tiny_node):
    assert NodeStep().check(_env(temp_home, node=tiny_node)).ok


def test_a_node_that_does_not_answer_says_to_check_the_vpn(temp_home):
    line = NodeStep().check(_env(temp_home, node="http://127.0.0.1:9", timeout_s=2))
    assert not line.ok and "VPN" in line.fix
    assert NodeStep().plan(_env(temp_home)) is None, "setup cannot connect a VPN for anyone"


def test_a_node_address_with_a_path_is_refused_with_the_bare_form(temp_home):
    line = NodeStep().check(_env(temp_home, node="https://node.example.org/ingest"))
    assert not line.ok and "nothing after the host" in line.fix


def test_no_node_yet_says_how_to_give_one(temp_home):
    line = NodeStep().check(_env(temp_home))
    assert not line.ok and "--node" in line.fix


# --------------------------------------------------------------------------- sign-in, end to end


@pytest.fixture
def gate(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.webgate.api import routers

    keys = tmp_path / "node" / "api-keys.json"
    keys.parent.mkdir()
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(keys))
    claims = {"sub": "u-1", "email": "someone@example.edu", "roles": ["viewer"], "site": "example"}
    monkeypatch.setattr(routers, "session_from_cookies", lambda *_a, **_k: claims)
    app = FastAPI()
    app.include_router(routers.build_webgate_router(oidc=None, secure_cookies=False))
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def laptop_vault(tmp_path):
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.extensions.builtins.secrets.providers.localfile import FileSecretStoreProvider

    values = FileSecretStoreProvider({"name": "t", "path": str(tmp_path / "v" / "values.json")}).open()
    return ForeignCredentialStore(tmp_path / "v", value_store=values)


def _gate_seams(gate, laptop_vault):
    import re
    import urllib.request
    from html import unescape

    def http_status(url, *, timeout, headers=None):
        reply = gate.get(urlsplit(url).path, headers=headers or {})
        return reply.status_code, dict(reply.headers), ""

    def browser(url):
        page = gate.get(urlsplit(url).path + "?" + urlsplit(url).query)
        fields = {unescape(a): unescape(b) for a, b in
                  re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page.text)}
        approved = gate.post("/gate/cli/approve", data=fields)
        urllib.request.urlopen(approved.headers["location"], timeout=10).read()  # noqa: S310
        return True

    def post_json(url, body, _timeout):
        reply = gate.post(urlsplit(url).path, json=body)
        return reply.status_code, reply.json()

    return {"store": laptop_vault, "http_status": http_status, "open_browser": browser,
            "post_json": post_json}


def test_signing_in_puts_a_working_key_in_the_vault_and_proves_it(temp_home, gate, laptop_vault):
    """Gets a key, then one real authenticated request as proof the node accepts it."""
    env = _env(temp_home, node="http://testserver", signin_timeout_s=10,
               seams=_gate_seams(gate, laptop_vault))
    step = KeyStep()
    assert not step.check(env).ok
    line = run_steps(env, [step], ask=lambda _q: True)["lines"][0]
    assert line.ok, line
    assert "accepts your key" in line.status and "example" in line.status
    with laptop_vault.get("testserver-key") as secret:
        token = secret.as_str()
    assert token not in "\n".join(env.said) and token not in line.status
    assert env.site == "example", "the site the key is for was not learned"


def test_a_key_the_node_no_longer_accepts_is_red_and_setup_replaces_it(temp_home, gate, laptop_vault):
    laptop_vault.set("testserver-key", b"axk_revoked_or_never_issued", provider="axiom-api-key")
    env = _env(temp_home, node="http://testserver", signin_timeout_s=10,
               seams=_gate_seams(gate, laptop_vault))
    line = KeyStep().check(env)
    assert not line.ok and "no longer accepts" in line.status
    assert run_steps(env, [KeyStep()], ask=lambda _q: True)["lines"][0].ok


def test_an_invitation_signs_in_without_a_browser(temp_home, gate, laptop_vault, tmp_path):
    from datetime import timedelta

    from axiom.webauth.invitations import append_invitation, mint_invitation

    code, record = mint_invitation(principal="@guest:example", roles=["viewer"], scopes=["*:read"],
                                   site="example", expires_in=timedelta(days=1))
    append_invitation(tmp_path / "node" / "invitations.json", record)
    seams = _gate_seams(gate, laptop_vault)
    seams["open_browser"] = lambda _u: pytest.fail("opened a browser for an invitation")
    seams["invitation_code"] = code
    env = _env(temp_home, node="http://testserver", seams=seams)
    line = run_steps(env, [KeyStep()], ask=lambda _q: True)["lines"][0]
    assert line.ok, line
    assert "invitation_code" not in env.seams, "the code outlived its use"
    assert code not in "\n".join(env.said)


# --------------------------------------------------------------------------- AI apps


def test_only_the_ai_apps_found_are_connected(temp_home, monkeypatch):
    cfg = temp_home / "claude.json"
    monkeypatch.setenv("AXIOM_CLAUDE_CONFIG", str(cfg))
    env = _env(temp_home, seams={"detect_tools": lambda: {"claude-code": True, "cursor": False}})
    step = McpStep()
    assert "Claude Code" in step.plan(env) and "Cursor" not in step.plan(env)
    line = run_steps(env, [step], ask=lambda _q: True)["lines"][0]
    assert line.ok, line
    assert "mcpServers" in json.loads(cfg.read_text())
    assert not (temp_home / ".cursor").exists()


def test_with_no_ai_apps_the_line_is_not_red(temp_home):
    line = McpStep().check(_env(temp_home, seams={"detect_tools": lambda: {}}))
    assert line.skipped and line.green


def test_doctor_answers_help_instead_of_running_a_diagnosis(temp_home):
    """`doctor --help` used to run the whole diagnosis, model call included, and exit 1."""
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-m", "axiom.axiom_cli", "doctor", "--help"],
                          capture_output=True, text=True, timeout=180,
                          env={**__import__("os").environ, "AXI_DIAGNOSES_QUIET": "1"})
    assert done.returncode == 0, done.stdout[-800:] + done.stderr[-800:]
    assert "usage:" in done.stdout and "setup" in done.stdout
    assert "Analyzing" not in done.stdout
