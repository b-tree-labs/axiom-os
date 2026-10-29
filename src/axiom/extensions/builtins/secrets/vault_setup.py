# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Set up this computer's local vault: OpenBao in Docker, on macOS, Linux or Windows.

This is the logic that used to live in a bash script. It is Python so it runs the same on every
host OS, with the host-side work (finding Docker, restricting files, talking to the server) done
through the standard library and ``docker`` rather than ``curl``, ``wget``, ``chmod`` or ``id``.
The one thing that stays POSIX shell is the container's start script: the container runs Linux
whatever the host is.

The vault listens on 127.0.0.1 only, keeps its data under ``<state>/openbao``, and unseals itself
whenever Docker starts it (OpenBao always starts sealed): the container's start script reads the
unseal key from a file only the owner can read. Nothing here prints a secret.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .shell_profiles import Runner, system_name

DEFAULT_CONTAINER = "axi-openbao"
DEFAULT_PORT = 8200
IMAGE = "openbao/openbao:latest"

#: Runs inside the (Linux) container, so POSIX sh whatever the host is. Always written with LF.
START_SH = """#!/bin/sh
bao server -config=/openbao/config/config.hcl &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null' TERM INT
if [ -s /openbao/unseal/key ]; then
  i=0
  while [ "$i" -lt 60 ]; do
    if wget -q -O /tmp/seal http://127.0.0.1:8200/v1/sys/seal-status 2>/dev/null; then
      if grep -q '"sealed":true' /tmp/seal; then
        (umask 077; printf '{"key":"%s"}' "$(cat /openbao/unseal/key)" > /tmp/unseal.json)
        wget -q -O /dev/null --post-file=/tmp/unseal.json http://127.0.0.1:8200/v1/sys/unseal 2>/dev/null || true
        rm -f /tmp/unseal.json
      fi
      break
    fi
    i=$((i + 1)); sleep 1
  done
  rm -f /tmp/seal
fi
wait "$pid"
"""

CONFIG_HCL = """storage "raft" {
  path    = "/openbao/file"
  node_id = "axi-local"
}
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1
}
api_addr      = "http://127.0.0.1:8200"
cluster_addr  = "http://127.0.0.1:8201"
disable_mlock = true
"""


class SetupError(RuntimeError):
    """Setup cannot continue; the message says what to do."""


# ---------------------------------------------------------------- docker


def docker_hint(system: str, *, installed: bool) -> str:
    if not installed:
        return {
            "darwin": "Docker is not installed. Install Docker Desktop for Mac "
            "(https://docs.docker.com/desktop/setup/install/mac-install/), start it, "
            "and run this again.",
            "windows": "Docker is not installed. Install Docker Desktop for Windows "
            "(https://docs.docker.com/desktop/setup/install/windows-install/; it uses "
            "WSL 2), start it, and run this again.",
        }.get(
            system,
            "Docker is not installed. Install Docker Engine "
            "(https://docs.docker.com/engine/install/) and run this again.",
        )
    return {
        "darwin": "Docker is installed but not running. Open Docker Desktop, wait until it says "
        "it is running, and run this again.",
        "windows": "Docker is installed but not running. Start Docker Desktop, wait until it says "
        "it is running, and run this again.",
    }.get(
        system,
        "Docker is installed but not running, or not allowed for your user. Start it "
        "(sudo systemctl start docker) and make sure your user is in the docker group "
        "(sudo usermod -aG docker $USER, then sign in again).",
    )


def check_docker(
    system: str | None = None,
    *,
    which: Callable[[str], str | None] | None = None,
    run: Runner = subprocess.run,
) -> str | None:
    """``None`` when Docker is usable, else the sentence telling the person what to do."""
    system = system or system_name()
    find = which or shutil.which
    if not find("docker"):
        return docker_hint(system, installed=False)
    try:
        done = run(["docker", "info"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return docker_hint(system, installed=True)
    return None if done.returncode == 0 else docker_hint(system, installed=True)


def user_args(system: str | None = None) -> list[str]:
    """``--user uid:gid`` on Linux only, so the server can write a folder only you can read.

    Docker Desktop (macOS, Windows) maps ownership itself, and Windows has no uid to give.
    """
    if (system or system_name()) != "linux" or not hasattr(os, "getuid"):
        return []
    return ["--user", f"{os.getuid()}:{os.getgid()}"]


# ---------------------------------------------------------------- permissions


def restrict_to_owner(
    path: Path, system: str | None = None, *, run: Runner = subprocess.run, directory: bool = False
) -> tuple[bool, str]:
    """Make ``path`` readable by the current user alone. Returns ``(enforced, plain statement)``.

    POSIX: mode 0600 (0700 for a folder). Windows: the inherited permissions are removed and the
    current user alone is granted access, with ``icacls``. If that fails the statement says the file
    is NOT restricted, rather than implying it is.
    """
    system = system or system_name()
    if system != "windows":
        try:
            os.chmod(path, 0o700 if directory else 0o600)
        except OSError as exc:
            return False, f"could not restrict {path} to your user ({exc.__class__.__name__})"
        return True, f"{path} is readable by your user only"
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    domain = os.environ.get("USERDOMAIN")
    who = f"{domain}\\{user}" if domain and user else user
    if not who:
        return (
            False,
            f"could not tell which Windows user you are, so {path} is NOT restricted to you",
        )
    grant = f"{who}:(OI)(CI)F" if directory else f"{who}:F"
    try:
        done = run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False, f"icacls is not available, so {path} is NOT restricted to your user"
    if done.returncode != 0:
        return (
            False,
            f"icacls refused ({done.returncode}), so {path} is NOT restricted to your user",
        )
    return True, f"{path} is restricted to your Windows user (icacls)"


def _write_private(path: Path, text: str, system: str, run: Runner, notes: list[str]) -> None:
    """Create ``path`` private from the start where the OS allows, LF line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    ok, said = restrict_to_owner(path, system, run=run)
    if not ok:
        notes.append(said)


# ---------------------------------------------------------------- the server


class Vault:
    """The local server over HTTP with the standard library. Replaceable in tests."""

    def __init__(self, addr: str) -> None:
        self.addr = addr

    def call(self, method: str, path: str, body: Any = None, token: str | None = None) -> Any:
        req = urllib.request.Request(
            self.addr + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Vault-Token": token} if token else {},
        )
        with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310 - 127.0.0.1 http
            raw = resp.read()
            return json.loads(raw) if raw else {}

    def reachable(self) -> bool:
        try:
            self.call("GET", "/v1/sys/health")
            return True
        except urllib.error.HTTPError:
            return True  # sealed/standby answer with a status code: the server is there
        except (urllib.error.URLError, OSError):
            return False


def _containers(run: Runner) -> list[str]:
    done = run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True)
    return [n.strip() for n in (done.stdout or "").splitlines()]


def _mounts_unseal(run: Runner, name: str) -> bool:
    done = run(
        ["docker", "inspect", "-f", "{{range .Mounts}}{{.Destination}} {{end}}", name],
        capture_output=True,
        text=True,
    )
    return "/openbao/unseal" in (done.stdout or "")


def setup(
    *,
    state_dir: Path,
    container: str = DEFAULT_CONTAINER,
    port: int = DEFAULT_PORT,
    system: str | None = None,
    run: Runner = subprocess.run,
    vault_factory: Callable[[str], Any] = Vault,
    wait_s: int = 30,
    sleep: Callable[[float], None] = time.sleep,
    write_settings: Callable[..., Path] | None = None,
) -> dict[str, Any]:
    """Start the vault, initialise and unseal it, issue the kv-only token, record where it is.

    Returns ``{addr, dir, token_file, settings_file, actions, notes}``. Raises :class:`SetupError`
    with a sentence for the person when Docker is unusable or the server does not come up.
    """
    system = system or system_name()
    problem = check_docker(system, run=run)
    if problem:
        raise SetupError(problem)

    d = state_dir / "openbao"
    addr = f"http://127.0.0.1:{port}"
    actions: list[str] = []
    notes: list[str] = []

    for sub in ("data", "unseal"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    ok, said = restrict_to_owner(d, system, run=run, directory=True)
    if not ok:
        notes.append(said)
    _write_private(d / "start.sh", START_SH, system, run, notes)
    if not (d / "config.hcl").exists():
        _write_private(d / "config.hcl", CONFIG_HCL, system, run, notes)

    # A vault that never finished its first start holds nothing yet: start it clean.
    if not (d / "init.json").exists():
        run(["docker", "rm", "-f", container], capture_output=True, text=True)
        for sub in ("data", "unseal"):
            shutil.rmtree(d / sub, ignore_errors=True)
            (d / sub).mkdir(parents=True, exist_ok=True)

    existing = container in _containers(run)
    if existing and not _mounts_unseal(run, container):
        run(["docker", "rm", "-f", container], capture_output=True, text=True)
        existing = False
        actions.append("vault container updated so it unseals itself after a restart")
    if not existing:
        # --entrypoint sh runs our start script instead of the image's, which tries to chown the
        # data folder and is refused when the container runs as you.
        done = run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--restart",
                "unless-stopped",
                *user_args(system),
                "--entrypoint",
                "sh",
                "-p",
                f"127.0.0.1:{port}:8200",
                "-v",
                f"{d / 'config.hcl'}:/openbao/config/config.hcl:ro",
                "-v",
                f"{d / 'start.sh'}:/openbao/start.sh:ro",
                "-v",
                f"{d / 'unseal'}:/openbao/unseal:ro",
                "-v",
                f"{d / 'data'}:/openbao/file",
                IMAGE,
                "/openbao/start.sh",
            ],
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            last = ((done.stderr or "").strip().splitlines() or ["no message"])[-1]
            raise SetupError(f"Docker could not start the vault: {last}")
    else:
        run(["docker", "start", container], capture_output=True, text=True)

    vault = vault_factory(addr)
    for _ in range(wait_s):
        if vault.reachable():
            break
        sleep(1)
    else:
        logs = run(["docker", "logs", container], capture_output=True, text=True)
        tail = "\n".join(((logs.stdout or "") + (logs.stderr or "")).splitlines()[-8:])
        raise SetupError(
            "The vault did not start. Its last log lines:\n"
            + tail
            + f"\nRemove it (docker rm -f {container}), update, and run this again."
        )

    init_path, token_path = d / "init.json", d / "axi-token"
    if not vault.call("GET", "/v1/sys/init")["initialized"]:
        init = vault.call("PUT", "/v1/sys/init", {"secret_shares": 1, "secret_threshold": 1})
        _write_private(init_path, json.dumps(init), system, run, notes)
        actions.append("vault initialised")
    init = json.loads(init_path.read_text(encoding="utf-8"))
    key_path = d / "unseal" / "key"
    if not key_path.exists():
        # The container reads this at start-up to unseal itself (see START_SH).
        _write_private(key_path, init["keys"][0], system, run, notes)
    if vault.call("GET", "/v1/sys/seal-status")["sealed"]:
        vault.call("PUT", "/v1/sys/unseal", {"key": init["keys"][0]})
        actions.append("vault unsealed")
    root = init["root_token"]
    if "kv/" not in vault.call("GET", "/v1/sys/mounts", token=root):
        vault.call("POST", "/v1/sys/mounts/kv", {"type": "kv", "options": {"version": "2"}}, root)
        vault.call(
            "PUT",
            "/v1/sys/policies/acl/axi",
            {"policy": 'path "kv/*" { capabilities = ["create","read","update","delete","list"] }'},
            root,
        )
    if not token_path.exists():
        tok = vault.call(
            "POST",
            "/v1/auth/token/create",
            {"policies": ["axi"], "ttl": "8760h", "renewable": True, "display_name": "axi-local"},
            root,
        )
        _write_private(token_path, tok["auth"]["client_token"], system, run, notes)
        actions.append("vault token issued (kv only)")
    actions.append(f"vault ready at {addr}")

    if write_settings is None:
        from . import vault_settings

        write_settings = vault_settings.write
    settings = write_settings(backend="openbao", url=addr, token_file=str(token_path), mount="kv")
    actions.append(f"vault settings recorded in {settings} (for apps started outside a terminal)")
    return {
        "addr": addr,
        "dir": str(d),
        "token_file": str(token_path),
        "settings_file": str(settings),
        "actions": actions,
        "notes": notes,
    }


__all__ = [
    "CONFIG_HCL",
    "START_SH",
    "SetupError",
    "Vault",
    "check_docker",
    "docker_hint",
    "restrict_to_owner",
    "setup",
    "user_args",
]
