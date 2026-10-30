# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``secrets vault setup`` works on macOS, Windows and Linux, in whichever shell a person uses.

The vault used to be set up by a bash script that edited ``~/.zshrc`` or a bash profile and nothing
else. A person on fish, PowerShell, nushell or cmd.exe, or on Windows at all, had no path. These
tests hold the replacement: the block each shell gets, that writing it twice changes nothing, that a
block the old script wrote is recognised, that the token is never a value in a profile, how files
are made private on each OS, and what Docker problems are called.

They run with the operating system mocked so one runner covers all three, and CI also runs them
on real ubuntu, macos and windows runners.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.secrets import shell_profiles as sp
from axiom.extensions.builtins.secrets import vault_settings, vault_setup
from axiom.infra.skills import SkillContext, default_registry

URL = "http://127.0.0.1:8200"
TOKEN_FILE = "/home/u/.axi/openbao/axi-token"
TOKEN_VALUE = "hvs.FAKE-TOKEN-VALUE-never-in-a-profile"

BEGIN, END = sp.markers("Exampleware")

GOLDEN = {
    "zsh": f"""{BEGIN}
export AXIOM_FOREIGN_SECRETS_BACKEND=openbao
export AXIOM_OPENBAO_URL={URL}
export AXIOM_OPENBAO_TOKEN="$(cat {TOKEN_FILE})"
{END}
""",
    "fish": f"""{BEGIN}
set -gx AXIOM_FOREIGN_SECRETS_BACKEND openbao
set -gx AXIOM_OPENBAO_URL {URL}
set -gx AXIOM_OPENBAO_TOKEN (cat {TOKEN_FILE})
{END}
""",
    "pwsh": f"""{BEGIN}
$env:AXIOM_FOREIGN_SECRETS_BACKEND = "openbao"
$env:AXIOM_OPENBAO_URL = "{URL}"
if (Test-Path -LiteralPath '{TOKEN_FILE}') {{ $env:AXIOM_OPENBAO_TOKEN = (Get-Content -Raw -LiteralPath '{TOKEN_FILE}').Trim() }}
{END}
""",
    "nu": f"""{BEGIN}
$env.AXIOM_FOREIGN_SECRETS_BACKEND = "openbao"
$env.AXIOM_OPENBAO_URL = "{URL}"
$env.AXIOM_OPENBAO_TOKEN = (open --raw "{TOKEN_FILE}" | str trim)
{END}
""",
}
GOLDEN["bash"] = GOLDEN["sh"] = GOLDEN["zsh"]
GOLDEN["powershell"] = GOLDEN["pwsh"]


@pytest.fixture(autouse=True)
def _brand(monkeypatch):
    """The consumer layer's brand, as the CLI would resolve it."""
    monkeypatch.setattr(sp, "product_name", lambda: "Exampleware")


# ---------------------------------------------------------------- the block each shell gets


@pytest.mark.parametrize("shell", sorted(GOLDEN))
def test_each_shell_gets_its_own_syntax(shell):
    assert sp.render(shell, url=URL, token_file=TOKEN_FILE, product="Exampleware") == GOLDEN[shell]


@pytest.mark.parametrize("shell", sorted(GOLDEN))
def test_the_token_is_read_from_its_file_never_written_as_a_value(shell, tmp_path):
    token_file = tmp_path / "axi-token"
    token_file.write_text(TOKEN_VALUE)
    block = sp.render(shell, url=URL, token_file=str(token_file))
    assert TOKEN_VALUE not in block
    assert "axi-token" in block, "the block must read the token file at shell start"


def test_a_path_with_spaces_is_quoted_for_posix_shells():
    block = sp.render("zsh", url=URL, token_file="/Users/a b/.axi/openbao/axi-token")
    assert '"$(cat "/Users/a b/.axi/openbao/axi-token")"' in block


def test_cmd_has_no_profile_syntax():
    with pytest.raises(ValueError):
        sp.render("cmd", url=URL, token_file=TOKEN_FILE)


# ---------------------------------------------------------------- where each profile is


@pytest.mark.parametrize(
    "shell, system, expected",
    [
        ("zsh", "darwin", ".zshrc"),
        ("bash", "darwin", ".bash_profile"),
        ("bash", "linux", ".bashrc"),
        ("bash", "windows", ".bash_profile"),  # Git Bash
        ("sh", "linux", ".profile"),
        ("fish", "linux", ".config/fish/conf.d/exampleware-vault.fish"),
        ("pwsh", "linux", ".config/powershell/profile.ps1"),
        ("pwsh", "darwin", ".config/powershell/profile.ps1"),
        ("pwsh", "windows", "Documents/PowerShell/profile.ps1"),
        ("powershell", "windows", "Documents/WindowsPowerShell/profile.ps1"),
        ("nu", "linux", ".config/nushell/env.nu"),
    ],
)
def test_profile_locations(shell, system, expected, tmp_path, monkeypatch):
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)
    monkeypatch.setattr(sp, "product_name", lambda: "Exampleware")
    got = sp.profile_path(shell, home=tmp_path, system=system, run=None)
    assert got == tmp_path / Path(expected)


def test_windows_powershell_5_does_not_exist_elsewhere_and_cmd_has_no_profile(tmp_path):
    assert sp.profile_path("powershell", home=tmp_path, system="linux", run=None) is None
    assert sp.profile_path("cmd", home=tmp_path, system="windows", run=None) is None


def test_powershell_is_asked_where_its_profile_is(tmp_path, monkeypatch):
    redirected = tmp_path / "OneDrive" / "Documents" / "PowerShell" / "profile.ps1"
    monkeypatch.setattr(sp.shutil, "which", lambda name: "/x/pwsh")
    asked = []

    def run(cmd, **kw):
        asked.append(cmd)
        return SimpleNamespace(returncode=0, stdout=f"{redirected}\n")

    assert sp.profile_path("pwsh", home=tmp_path, system="windows", run=run) == redirected
    assert "$PROFILE.CurrentUserAllHosts" in asked[0]


# ---------------------------------------------------------------- writing: add, idempotent, update


def test_adding_creates_a_missing_profile_and_its_folders(tmp_path):
    path = tmp_path / ".config" / "fish" / "conf.d" / "exampleware-vault.fish"
    done = sp.apply("fish", path, GOLDEN["fish"])
    assert done.outcome == "added" and done.backup is None
    assert path.read_text() == GOLDEN["fish"]


def test_adding_keeps_what_was_already_there_and_backs_it_up(tmp_path):
    path = tmp_path / ".zshrc"
    path.write_text("export PATH=$HOME/bin:$PATH\nalias ll='ls -l'")  # no trailing newline
    done = sp.apply("zsh", path, GOLDEN["zsh"], now=datetime(2026, 10, 28, 12, 0, 0))
    assert done.outcome == "added"
    text = path.read_text()
    assert text.startswith("export PATH=$HOME/bin:$PATH\nalias ll='ls -l'\n")
    assert text.endswith(GOLDEN["zsh"])
    assert done.backup.name == ".zshrc.vault-setup-bak-20261028-120000"
    assert done.backup.read_text() == "export PATH=$HOME/bin:$PATH\nalias ll='ls -l'"


@pytest.mark.parametrize("shell", ["zsh", "fish", "pwsh", "nu"])
def test_running_it_twice_changes_nothing(shell, tmp_path):
    path = tmp_path / "profile"
    path.write_text("# mine\n")
    sp.apply(shell, path, GOLDEN[shell])
    once = path.read_text()
    again = sp.apply(shell, path, GOLDEN[shell])
    assert again.outcome == "unchanged" and again.backup is None
    assert path.read_text() == once
    assert once.count(BEGIN) == 1
    assert len(list(tmp_path.glob("profile.vault-setup-bak-*"))) == 1, (
        "only the first edit backs up"
    )


def test_a_changed_address_is_updated_in_place_not_appended(tmp_path):
    path = tmp_path / ".zshrc"
    path.write_text("before\n")
    sp.apply("zsh", path, sp.render("zsh", url=URL, token_file=TOKEN_FILE))
    path.write_text(path.read_text() + "after\n")
    done = sp.apply(
        "zsh", path, sp.render("zsh", url="http://127.0.0.1:9999", token_file=TOKEN_FILE)
    )
    assert done.outcome == "updated" and done.backup is not None
    text = path.read_text()
    assert text.count(BEGIN) == 1 and "9999" in text and "8200" not in text
    assert text.startswith("before\n") and text.endswith("after\n"), "surrounding lines untouched"


def test_the_block_the_old_script_wrote_is_recognised_as_the_same_block(tmp_path):
    """Setup must not stack a second block on a machine already set up by the bash script."""
    path = tmp_path / ".zshrc"
    old = (
        "export EDITOR=vim\n\n"
        "# >>> Exampleware vault (added by start-local-vault.sh) >>>\n"
        "export AXIOM_FOREIGN_SECRETS_BACKEND=openbao\n"
        f"export AXIOM_OPENBAO_URL={URL}\n"
        f'export AXIOM_OPENBAO_TOKEN="$(cat {TOKEN_FILE})"\n'
        "# <<< Exampleware vault <<<\n"
    )
    path.write_text(old)
    done = sp.apply("zsh", path, GOLDEN["zsh"])
    assert done.outcome == "unchanged"
    assert path.read_text() == old


def test_a_block_under_another_product_name_is_the_same_block_and_keeps_its_labels(tmp_path):
    path = tmp_path / ".zshrc"
    path.write_text(GOLDEN["zsh"])
    same = sp.apply("zsh", path, sp.render("zsh", url=URL, token_file=TOKEN_FILE, product="Axiom"))
    assert same.outcome == "unchanged" and path.read_text() == GOLDEN["zsh"]

    moved = sp.apply(
        "zsh",
        path,
        sp.render("zsh", url="http://127.0.0.1:9999", token_file=TOKEN_FILE, product="Axiom"),
    )
    text = path.read_text()
    assert moved.outcome == "updated" and "9999" in text
    assert text.count("vault (added by") == 1 and BEGIN in text and END in text


def test_a_crlf_block_is_found_and_updated(tmp_path):
    path = tmp_path / "profile.ps1"
    path.write_bytes(GOLDEN["pwsh"].replace("\n", "\r\n").encode())
    done = sp.apply("pwsh", path, GOLDEN["pwsh"])
    assert done.outcome == "unchanged"


def test_a_start_marker_without_an_end_marker_is_refused_and_nothing_is_touched(tmp_path):
    path = tmp_path / ".zshrc"
    broken = f"mine\n{BEGIN}\nexport X=1\nmore of my own config\n"
    path.write_text(broken)
    done = sp.apply("zsh", path, GOLDEN["zsh"])
    assert done.outcome == "refused" and str(path) in done.detail
    assert path.read_text() == broken
    assert not list(tmp_path.glob("*.vault-setup-bak-*"))


def test_windows_line_endings_are_kept(tmp_path):
    path = tmp_path / "profile.ps1"
    path.write_bytes(b"# mine\r\nSet-Alias x y\r\n")
    sp.apply("pwsh", path, GOLDEN["pwsh"])
    data = path.read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


# ---------------------------------------------------------------- detection


def test_detection_finds_the_login_shell_and_the_shells_with_profiles(tmp_path, monkeypatch):
    (tmp_path / ".zshrc").write_text("")
    (tmp_path / ".config" / "fish" / "conf.d").mkdir(parents=True)
    (tmp_path / ".config" / "fish" / "conf.d" / "exampleware-vault.fish").write_text("")
    found = sp.detect(
        home=tmp_path,
        env={"SHELL": "/bin/zsh"},
        system="darwin",
        which=lambda n: None,
        run=None,
    )
    assert found == ["zsh", "fish"]


def test_detection_on_windows_offers_powershell_and_cmd(tmp_path):
    found = sp.detect(
        home=tmp_path,
        env={},
        system="windows",
        which=lambda n: f"C:/x/{n}.exe" if n in ("pwsh", "powershell") else None,
        run=None,
    )
    assert found == ["pwsh", "powershell", "cmd"]


def test_a_shell_that_is_not_installed_and_has_no_profile_is_not_offered(tmp_path):
    found = sp.detect(
        home=tmp_path, env={"SHELL": "/bin/bash"}, system="linux", which=lambda n: None, run=None
    )
    assert found == ["bash"]


# ---------------------------------------------------------------- cmd.exe


def test_cmd_saves_the_address_and_backend_and_never_the_token():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    lines = sp.cmd_setx(URL, run=run)
    assert [c[:2] for c in calls] == [["setx", sp.ENV_BACKEND], ["setx", sp.ENV_URL]]
    assert all("TOKEN" not in " ".join(c) for c in calls)
    assert len(lines) == 2
    assert "never" in sp.CMD_NOTE and "secrets.toml" in sp.CMD_NOTE and "PowerShell" in sp.CMD_NOTE


# ---------------------------------------------------------------- permissions per OS


@pytest.mark.skipif(not hasattr(__import__("os"), "getuid"), reason="POSIX modes")
def test_posix_files_are_0600_and_folders_0700(tmp_path):
    f, d = tmp_path / "k", tmp_path / "d"
    f.write_text("x")
    d.mkdir()
    ok, said = vault_setup.restrict_to_owner(f, "linux")
    ok2, _ = vault_setup.restrict_to_owner(d, "darwin", directory=True)
    assert ok and ok2 and "your user only" in said
    assert f.stat().st_mode & 0o777 == 0o600
    assert d.stat().st_mode & 0o777 == 0o700


def test_windows_restricts_with_icacls_to_the_current_user(tmp_path, monkeypatch):
    monkeypatch.setenv("USERNAME", "ben")
    monkeypatch.setenv("USERDOMAIN", "PC")
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return SimpleNamespace(returncode=0)

    ok, said = vault_setup.restrict_to_owner(tmp_path / "axi-token", "windows", run=run)
    assert ok and "icacls" in said
    assert seen == [
        ["icacls", str(tmp_path / "axi-token"), "/inheritance:r", "/grant:r", "PC\\ben:F"]
    ]


def test_windows_folder_grant_is_inherited_by_what_is_inside(tmp_path, monkeypatch):
    monkeypatch.setenv("USERNAME", "ben")
    monkeypatch.delenv("USERDOMAIN", raising=False)
    seen = []
    vault_setup.restrict_to_owner(
        tmp_path,
        "windows",
        directory=True,
        run=lambda c, **k: seen.append(c) or SimpleNamespace(returncode=0),
    )
    assert seen[0][-1] == "ben:(OI)(CI)F"


def test_when_windows_cannot_restrict_a_file_it_says_so_plainly(tmp_path, monkeypatch):
    monkeypatch.setenv("USERNAME", "ben")

    def missing(cmd, **kw):
        raise FileNotFoundError("icacls")

    ok, said = vault_setup.restrict_to_owner(tmp_path / "k", "windows", run=missing)
    assert not ok and "NOT restricted" in said

    ok, said = vault_setup.restrict_to_owner(
        tmp_path / "k", "windows", run=lambda c, **k: SimpleNamespace(returncode=5)
    )
    assert not ok and "NOT restricted" in said and "5" in said


def test_a_token_file_is_trusted_on_windows_where_modes_carry_no_meaning(tmp_path, monkeypatch):
    """Judging by st_mode there would refuse every token file, and the vault would be 'not found'."""
    f = tmp_path / "axi-token"
    f.write_text("t")
    monkeypatch.setattr(vault_settings.os, "name", "nt")
    assert vault_settings._owner_only(f) is True


# ---------------------------------------------------------------- docker


@pytest.mark.parametrize(
    "system, needle",
    [
        ("darwin", "Docker Desktop for Mac"),
        ("windows", "Docker Desktop for Windows"),
        ("linux", "Docker Engine"),
    ],
)
def test_missing_docker_is_named_per_os(system, needle):
    said = vault_setup.check_docker(system, which=lambda n: None)
    assert needle in said and "not installed" in said


@pytest.mark.parametrize(
    "system, needle",
    [
        ("darwin", "Open Docker Desktop"),
        ("windows", "Start Docker Desktop"),
        ("linux", "systemctl start docker"),
    ],
)
def test_docker_that_is_not_running_is_named_per_os(system, needle):
    said = vault_setup.check_docker(
        system, which=lambda n: "/usr/bin/docker", run=lambda c, **k: SimpleNamespace(returncode=1)
    )
    assert needle in said and "not running" in said


def test_working_docker_has_no_complaint():
    assert (
        vault_setup.check_docker(
            "linux", which=lambda n: "/x", run=lambda c, **k: SimpleNamespace(returncode=0)
        )
        is None
    )


def test_user_flag_only_on_linux():
    if hasattr(__import__("os"), "getuid"):
        assert vault_setup.user_args("linux")[0] == "--user"
    else:
        assert vault_setup.user_args("linux") == [], "no uid to give on a Windows host"
    assert vault_setup.user_args("darwin") == []
    assert vault_setup.user_args("windows") == []


def test_the_container_script_is_posix_sh_with_unix_line_endings(tmp_path):
    assert vault_setup.START_SH.startswith("#!/bin/sh\n")
    assert "\r" not in vault_setup.START_SH
    assert "bash" not in vault_setup.START_SH


# ---------------------------------------------------------------- the whole setup, with fakes


class FakeVault:
    def __init__(self, addr):
        self.addr, self.initialized, self.sealed, self.calls = addr, False, True, []

    def reachable(self):
        return True

    def call(self, method, path, body=None, token=None):
        self.calls.append((method, path))
        if path == "/v1/sys/init" and method == "GET":
            return {"initialized": self.initialized}
        if path == "/v1/sys/init":
            self.initialized = True
            return {"keys": ["FAKE-UNSEAL-KEY"], "root_token": "FAKE-ROOT"}
        if path == "/v1/sys/seal-status":
            return {"sealed": self.sealed}
        if path == "/v1/sys/unseal":
            self.sealed = False
            return {}
        if path == "/v1/sys/mounts" and method == "GET":
            return {}
        if path == "/v1/auth/token/create":
            return {"auth": {"client_token": TOKEN_VALUE}}
        return {}


class FakeDocker:
    def __init__(self, existing=()):
        self.cmds, self.existing = [], list(existing)

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        out = ""
        if cmd[:3] == ["docker", "ps", "-a"]:
            out = "\n".join(self.existing)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


def _setup(tmp_path, system, docker=None, **kw):
    docker = docker or FakeDocker()
    vault = FakeVault("")
    res = vault_setup.setup(
        state_dir=tmp_path,
        system=system,
        run=docker,
        vault_factory=lambda a: vault,
        sleep=lambda s: None,
        write_settings=lambda **k: vault_settings_write(tmp_path, **k),
        **kw,
    )
    return res, docker, vault


def vault_settings_write(tmp_path, **k):
    p = tmp_path / "config" / "secrets.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(k))
    return p


def test_setup_starts_initialises_unseals_and_records_where_the_token_is(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_setup.shutil, "which", lambda n: "/usr/bin/docker")
    res, docker, vault = _setup(tmp_path, "darwin", port=18200, container="throwaway-vault")
    run_cmd = next(c for c in docker.cmds if c[:3] == ["docker", "run", "-d"])
    assert "throwaway-vault" in run_cmd and "127.0.0.1:18200:8200" in run_cmd
    assert "--user" not in run_cmd, "Docker Desktop maps ownership itself"
    assert vault.initialized and not vault.sealed
    d = tmp_path / "openbao"
    assert (d / "unseal" / "key").read_text() == "FAKE-UNSEAL-KEY"
    assert (d / "axi-token").read_text() == TOKEN_VALUE
    assert (d / "start.sh").read_bytes().startswith(b"#!/bin/sh\n") and b"\r" not in (
        d / "start.sh"
    ).read_bytes()
    settings = json.loads(Path(res["settings_file"]).read_text())
    assert settings["token_file"] == str(d / "axi-token") and TOKEN_VALUE not in json.dumps(
        settings
    )
    assert TOKEN_VALUE not in "\n".join(res["actions"] + res["notes"])


@pytest.mark.skipif(not hasattr(__import__("os"), "getuid"), reason="POSIX only")
def test_setup_on_linux_runs_the_container_as_the_user(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_setup.shutil, "which", lambda n: "/usr/bin/docker")
    _, docker, _ = _setup(tmp_path, "linux")
    run_cmd = next(c for c in docker.cmds if c[:3] == ["docker", "run", "-d"])
    assert "--user" in run_cmd


def test_setup_refuses_before_touching_anything_when_docker_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_setup.shutil, "which", lambda n: None)
    with pytest.raises(vault_setup.SetupError, match="Docker Desktop for Windows"):
        vault_setup.setup(state_dir=tmp_path, system="windows")
    assert not (tmp_path / "openbao").exists()


def test_setup_on_windows_restricts_every_secret_file_with_icacls(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_setup.shutil, "which", lambda n: "C:/docker.exe")
    monkeypatch.setenv("USERNAME", "ben")
    _, docker, _ = _setup(tmp_path, "windows")
    restricted = [c[1] for c in docker.cmds if c[0] == "icacls"]
    for name in ("axi-token", "init.json", "key", "start.sh"):
        assert any(r.endswith(name) for r in restricted), name


# ---------------------------------------------------------------- the verb


@pytest.fixture
def verb(tmp_path, monkeypatch):
    """The skill, with the server work stubbed and HOME pointed at a temp dir."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(sp, "markers", lambda product=None: (BEGIN, END))
    fake = {
        "addr": URL,
        "dir": "d",
        "token_file": TOKEN_FILE,
        "settings_file": str(tmp_path / "state" / "config" / "secrets.toml"),
        "actions": ["vault ready at " + URL],
        "notes": [],
    }
    monkeypatch.setattr(vault_setup, "setup", lambda **kw: dict(fake))
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    from axiom.extensions.builtins.secrets.skills import vault as vault_skill

    def call(params, system="darwin", prompt=None):
        ctx = SkillContext(
            registry=default_registry(),
            state_dir=tmp_path,
            logger=logging.getLogger("t"),
            user_prompt=prompt,
        )
        monkeypatch.setattr(sp.shutil, "which", lambda n: None)
        monkeypatch.setenv("SHELL", "/bin/zsh")
        return vault_skill.run(
            {"action": "setup", "_home": str(home), "_system": system, **params}, ctx
        )

    return SimpleNamespace(call=call, home=home)


def test_without_yes_and_without_a_terminal_it_prints_the_lines_and_changes_nothing(verb):
    r = verb.call({})
    assert r.ok
    assert not (verb.home / ".zshrc").exists()
    assert any("export AXIOM_OPENBAO_URL" in a for a in r.actions_taken)
    assert any("Not changing any shell profile" in a for a in r.actions_taken)


def test_yes_writes_the_login_shells_profile(verb):
    r = verb.call({"yes": True})
    assert (verb.home / ".zshrc").read_text() == GOLDEN["zsh"]
    assert r.value["profiles_changed"] == [str(verb.home / ".zshrc")]


def test_shell_list_writes_exactly_those_shells(verb):
    r = verb.call({"yes": True, "shell": "zsh,fish,pwsh"})
    assert r.ok
    assert (verb.home / ".zshrc").exists()
    assert (verb.home / ".config/fish/conf.d/exampleware-vault.fish").read_text() == GOLDEN["fish"]
    assert (verb.home / ".config/powershell/profile.ps1").read_text() == GOLDEN["pwsh"]
    assert not (verb.home / ".profile").exists()


def test_all_detected_writes_every_shell_found(verb):
    (verb.home / ".bash_profile").write_text("")
    verb.call({"yes": True, "all_detected": True})
    assert (verb.home / ".bash_profile").read_text() == GOLDEN["bash"]
    assert (verb.home / ".zshrc").read_text() == GOLDEN["zsh"]


def test_an_interactive_no_changes_nothing_and_shows_the_lines(verb):
    r = verb.call({"interactive": True}, prompt=lambda q: "n")
    assert not (verb.home / ".zshrc").exists()
    assert any("export AXIOM_OPENBAO_URL" in a for a in r.actions_taken)


def test_an_interactive_yes_edits(verb):
    verb.call({"interactive": True}, prompt=lambda q: "")
    assert (verb.home / ".zshrc").exists()


def test_an_unknown_shell_is_named_with_the_choices(verb):
    r = verb.call({"yes": True, "shell": "tcsh"})
    assert not r.ok and "tcsh" in r.errors[0] and "fish" in r.errors[0]


def test_cmd_on_windows_saves_url_and_backend_only_and_says_why(verb, monkeypatch):
    calls = []
    monkeypatch.setattr(sp, "cmd_setx", lambda url, run=None: calls.append(url) or ["saved"])
    r = verb.call({"yes": True, "shell": "cmd"}, system="windows")
    assert calls == [URL]
    text = "\n".join(r.actions_taken)
    assert "never put in the registry" in text and "secrets.toml" in text


def test_the_output_says_profiles_are_optional(verb):
    r = verb.call({})
    assert any("Shell profiles are optional" in a and "secrets.toml" in a for a in r.actions_taken)


def test_a_refused_profile_makes_the_verb_fail_loudly(verb):
    (verb.home / ".zshrc").write_text(f"{BEGIN}\nno end\n")
    r = verb.call({"yes": True})
    assert not r.ok and "end marker" in r.errors[0]


# ---------------------------------------------------------------- entry points find the vault with no env vars


def test_an_app_with_no_environment_finds_the_vault_from_secrets_toml(tmp_path, monkeypatch):
    for name in (
        vault_settings.ENV_BACKEND,
        vault_settings.ENV_OPENBAO_URL,
        vault_settings.ENV_OPENBAO_TOKEN,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    token = tmp_path / "axi-token"
    token.write_text(TOKEN_VALUE)
    if hasattr(__import__("os"), "getuid"):
        token.chmod(0o600)
    vault_settings.write(backend="openbao", url="http://127.0.0.1:18200", token_file=str(token))
    assert vault_settings.foreign_backend() == "openbao"
    assert vault_settings.openbao_url() == "http://127.0.0.1:18200"
    assert vault_settings.openbao_token() == TOKEN_VALUE
