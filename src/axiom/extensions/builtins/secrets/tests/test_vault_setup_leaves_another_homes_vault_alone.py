# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Setting up a vault never removes or replaces a container this home's setup did not create.

A first-hour release test on 0.70.19 ran ``setup`` in a fresh home on a computer that already
ran a vault. ``vault setup`` honoured ``AXI_OPENBAO_CONTAINER`` and ``AXI_OPENBAO_PORT``;
``setup``'s vault step did not, so it went for ``axi-openbao`` on 8200, and a fresh home (no
``init.json`` yet) removes that container to "start clean". It would have deleted another
home's vault.

Now both commands resolve the container and port in one place, and a container with the vault's
name is touched only when its mounts are this home's state folder. Every test runs against a
temporary home with a fake ``docker``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.secrets import vault_setup

TOKEN_VALUE = "hvs.FAKE-TOKEN-VALUE-for-tests-only"


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AXI_STATE_DIR", str(home / ".axi"))
    for var in (
        vault_setup.CONTAINER_ENV,
        vault_setup.PORT_ENV,
        "AXIOM_FOREIGN_SECRETS_BACKEND",
        "AXIOM_OPENBAO_URL",
        "AXIOM_OPENBAO_TOKEN",
        "AXI_SETUP_RESUMED",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


class FakeVault:
    def __init__(self, addr=""):
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


class FakeDocker:
    """``docker`` with named containers, each mounting the folders it was given."""

    def __init__(self, containers: dict[str, list[str]] | None = None, *, inspect_fails=False):
        self.containers = dict(containers or {})
        self.cmds: list[list[str]] = []
        self.inspect_fails = inspect_fails

    def __call__(self, cmd, **kw):
        self.cmds.append(list(cmd))
        out, code = "", 0
        if cmd[:3] == ["docker", "ps", "-a"]:
            out = "\n".join(self.containers)
        elif cmd[:2] == ["docker", "inspect"]:
            name = cmd[-1]
            if self.inspect_fails or name not in self.containers:
                code = 1
            elif ".Source" in cmd[3]:
                out = "\n".join(self.containers[name]) + "\n"
            else:
                out = " ".join(dst for dst in ("/openbao/file", "/openbao/unseal")) + " "
        elif cmd[:3] == ["docker", "rm", "-f"]:
            self.containers.pop(cmd[3], None)
        elif cmd[:3] == ["docker", "run", "-d"]:
            name = cmd[cmd.index("--name") + 1]
            self.containers[name] = [a.split(":")[0] for a in cmd if a.startswith("/")]
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    def did(self, *verb: str) -> list[list[str]]:
        return [c for c in self.cmds if c[1 : 1 + len(verb)] == list(verb)]


def _settings(tmp_path):
    def write(**k):
        p = tmp_path / "secrets.json"
        p.write_text(json.dumps(k))
        return p

    return write


def _setup(state_dir: Path, docker: FakeDocker, **kw):
    return vault_setup.setup(
        state_dir=state_dir,
        system="darwin",
        run=docker,
        vault_factory=lambda a: FakeVault(a),
        sleep=lambda s: None,
        which=lambda n: f"/usr/bin/{n}",
        write_settings=_settings(state_dir),
        **kw,
    )


def _other_home(tmp_path) -> list[str]:
    other = tmp_path / "someone-else" / ".axi" / "openbao"
    (other / "data").mkdir(parents=True)
    return [str(other / "data"), str(other / "unseal"), str(other / "config.hcl")]


# ---------------------------------------------------------------- whose container is it


def test_an_existing_axi_openbao_for_another_home_is_left_alone(temp_home, tmp_path):
    docker = FakeDocker({"axi-openbao": _other_home(tmp_path)})
    with pytest.raises(vault_setup.ForeignContainer) as raised:
        _setup(temp_home / ".axi", docker)
    assert docker.did("rm") == [] and docker.did("run") == [] and docker.did("start") == []
    assert "axi-openbao" in docker.containers, "the other home's vault is still there"
    said = str(raised.value)
    assert vault_setup.CONTAINER_ENV in said and vault_setup.PORT_ENV in said
    assert "someone-else" in said, "it says whose state the container keeps"


def test_a_container_that_cannot_say_what_it_mounts_is_left_alone(temp_home, tmp_path):
    docker = FakeDocker({"axi-openbao": _other_home(tmp_path)}, inspect_fails=True)
    with pytest.raises(vault_setup.ForeignContainer):
        _setup(temp_home / ".axi", docker)
    assert docker.did("rm") == [] and docker.did("run") == []


def test_this_homes_own_container_is_started_not_replaced(temp_home):
    state = temp_home / ".axi"
    d = state / "openbao"
    d.mkdir(parents=True)
    (d / "init.json").write_text(json.dumps({"keys": ["K"], "root_token": "R"}))
    docker = FakeDocker({"axi-openbao": [str(d / "data"), str(d / "unseal")]})
    _setup(state, docker)
    assert docker.did("rm") == [] and docker.did("run") == []
    assert docker.did("start") == [["docker", "start", "axi-openbao"]]


def test_this_homes_half_made_container_is_removed_and_made_again(temp_home):
    """No init.json: this home's vault never finished its first start and holds nothing."""
    state = temp_home / ".axi"
    d = state / "openbao"
    docker = FakeDocker({"axi-openbao": [str(d / "data")]})
    _setup(state, docker)
    assert docker.did("rm", "-f") == [["docker", "rm", "-f", "axi-openbao"]]
    assert len(docker.did("run")) == 1


def test_a_fresh_home_with_no_container_creates_one_and_removes_nothing(temp_home):
    docker = FakeDocker()
    _setup(temp_home / ".axi", docker)
    assert docker.did("rm") == [] and len(docker.did("run")) == 1


# ---------------------------------------------------------------- one resolution of name and port


def test_the_overrides_choose_the_container_and_port(monkeypatch):
    monkeypatch.setenv(vault_setup.CONTAINER_ENV, "axi-openbao-ci")
    monkeypatch.setenv(vault_setup.PORT_ENV, "18230")
    assert vault_setup.resolve_target() == ("axi-openbao-ci", 18230)


def test_what_was_asked_for_beats_the_environment(monkeypatch):
    monkeypatch.setenv(vault_setup.CONTAINER_ENV, "from-env")
    monkeypatch.setenv(vault_setup.PORT_ENV, "18230")
    assert vault_setup.resolve_target(container="asked", port=18240) == ("asked", 18240)


def test_without_overrides_it_is_the_default():
    assert vault_setup.resolve_target(environ={}) == (
        vault_setup.DEFAULT_CONTAINER,
        vault_setup.DEFAULT_PORT,
    )


@pytest.mark.parametrize("bad", ["eighty", "0", "70000"])
def test_a_port_that_is_not_a_port_is_refused(bad):
    with pytest.raises(ValueError):
        vault_setup.resolve_target(environ={vault_setup.PORT_ENV: bad})


def test_the_vault_verb_and_setup_share_the_resolution():
    """One resolution, no copy: neither caller reads the variables itself."""
    import inspect

    from axiom.extensions.builtins.secrets.skills import vault as vault_skill
    from axiom.extensions.builtins.setup import steps

    for module in (vault_skill, steps):
        source = inspect.getsource(module)
        assert "resolve_target" in source
        assert '"AXI_OPENBAO_PORT"' not in source and '"AXI_OPENBAO_CONTAINER"' not in source


# ---------------------------------------------------------------- api_addr follows the port


def test_the_redirect_address_names_the_port_it_was_given(temp_home):
    state = temp_home / ".axi"
    docker = FakeDocker()
    _setup(state, docker, port=18200)
    config = (state / "openbao" / "config.hcl").read_text()
    assert 'api_addr      = "http://127.0.0.1:18200"' in config
    assert 'address     = "0.0.0.0:8200"' in config, "inside the container it still listens on 8200"
    run_cmd = docker.did("run")[0]
    assert "127.0.0.1:18200:8200" in run_cmd


def test_a_config_written_for_another_port_gets_its_redirect_corrected(temp_home):
    state = temp_home / ".axi"
    d = state / "openbao"
    d.mkdir(parents=True)
    (d / "config.hcl").write_text(vault_setup.config_hcl(8200))
    _setup(state, FakeDocker(), port=18210)
    config = (d / "config.hcl").read_text()
    assert 'api_addr      = "http://127.0.0.1:18210"' in config and "127.0.0.1:8200" not in config


# ---------------------------------------------------------------- without Docker


def test_a_binary_vault_does_not_adopt_another_vault_on_its_port(temp_home):
    """Something already answers on the port and was initialised by someone else."""
    other = FakeVault()
    other.initialized = True
    started = []
    with pytest.raises(vault_setup.SetupError, match=vault_setup.PORT_ENV):
        vault_setup.setup(
            state_dir=temp_home / ".axi",
            system="linux",
            run=FakeDocker(),
            vault_factory=lambda a: other,
            sleep=lambda s: None,
            write_settings=_settings(temp_home),
            runtime="binary",
            which=lambda n: "/usr/local/bin/bao" if n == "bao" else None,
            popen=lambda cmd, **kw: started.append(cmd),
        )
    assert started == []


# ---------------------------------------------------------------- setup's vault step


def _step_env(home: Path, docker: FakeDocker):
    from axiom.extensions.builtins.setup.environment import SetupEnv

    said: list[str] = []
    return SetupEnv(
        home=home,
        state_dir=home / ".axi",
        system="darwin",
        cli="axi",
        product="Axiom",
        say=said.append,
        seams={
            "run": docker,
            "vault_factory": FakeVault,
            "which": lambda n: f"/usr/bin/{n}",
            "sleep": lambda s: None,
            "write_settings": _settings(home),
        },
    )


def test_setup_uses_the_container_and_port_overrides(temp_home, monkeypatch):
    from axiom.extensions.builtins.setup.steps import VaultStep

    monkeypatch.setenv(vault_setup.CONTAINER_ENV, "axi-openbao-second")
    monkeypatch.setenv(vault_setup.PORT_ENV, "18250")
    docker = FakeDocker({"axi-openbao": ["/somewhere/else/openbao/data"]})
    monkeypatch.setattr(VaultStep, "check", lambda self, env: self.ok("checked"))
    VaultStep().apply(_step_env(temp_home, docker))
    (run_cmd,) = docker.did("run")
    assert run_cmd[run_cmd.index("--name") + 1] == "axi-openbao-second"
    assert "127.0.0.1:18250:8200" in run_cmd
    assert "axi-openbao" in docker.containers and docker.did("rm") == []
    config = (temp_home / ".axi" / "openbao" / "config.hcl").read_text()
    assert "http://127.0.0.1:18250" in config


def test_setup_stops_and_names_the_override_when_the_container_is_another_homes(
    temp_home, tmp_path
):
    from axiom.extensions.builtins.setup.steps import VaultStep

    docker = FakeDocker({"axi-openbao": _other_home(tmp_path)})
    line = VaultStep().apply(_step_env(temp_home, docker))
    assert not line.ok
    assert "another home" in line.status
    assert vault_setup.CONTAINER_ENV in line.fix and vault_setup.PORT_ENV in line.fix
    assert docker.did("rm") == [] and docker.did("run") == [] and docker.did("start") == []
