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

#: Inside the container the server always listens on 8200; Docker publishes that on the host
#: port. ``api_addr`` is the address the server redirects clients to, so it names the HOST port:
#: with ``--port 18200`` a redirect to 8200 sent the client to whatever else listens there.
CONFIG_HCL = """storage "raft" {{
  path    = "/openbao/file"
  node_id = "axi-local"
}}
listener "tcp" {{
  address     = "0.0.0.0:8200"
  tls_disable = 1
}}
api_addr      = "http://127.0.0.1:{port}"
cluster_addr  = "http://127.0.0.1:8201"
disable_mlock = true
"""

#: The environment variables that choose the container and the host port. ``vault setup`` and
#: ``setup``'s vault step both read them through :func:`resolve_target`, so they cannot differ.
CONTAINER_ENV = "AXI_OPENBAO_CONTAINER"
PORT_ENV = "AXI_OPENBAO_PORT"


def resolve_target(
    *,
    container: str | None = None,
    port: int | str | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[str, int]:
    """``(container name, host port)``: what was asked for, then the environment, then the default.

    Raises ``ValueError`` for a port that is not a number from 1 to 65535.
    """
    env = os.environ if environ is None else environ
    name = str(container or env.get(CONTAINER_ENV) or DEFAULT_CONTAINER).strip()
    raw = port or env.get(PORT_ENV) or DEFAULT_PORT
    number = int(str(raw).strip())
    if not 0 < number < 65536:
        raise ValueError(f"port {number} is not between 1 and 65535")
    return name or DEFAULT_CONTAINER, number


def config_hcl(port: int = DEFAULT_PORT) -> str:
    """The container's server config, with ``api_addr`` on the host port."""
    return CONFIG_HCL.format(port=port)


#: The same server run straight from the ``bao`` binary, as the person, when there is no
#: Docker. Storage is a folder only they can read; it listens on this computer only.
BINARY_CONFIG_HCL = """storage "raft" {{
  path    = "{data}"
  node_id = "axi-local"
}}
listener "tcp" {{
  address     = "127.0.0.1:{port}"
  tls_disable = 1
}}
api_addr      = "http://127.0.0.1:{port}"
cluster_addr  = "http://127.0.0.1:{cluster_port}"
disable_mlock = true
"""


def bao_binary(which: Callable[[str], str | None] | None = None) -> str | None:
    """The OpenBao server binary on PATH (``bao``), or ``None``."""
    return (which or shutil.which)("bao")


def binary_hint(system: str) -> str:
    """What to install when neither Docker nor the OpenBao binary is here."""
    how = {
        "darwin": "brew install openbao",
        "windows": "download bao.exe from https://openbao.org/downloads/ and put it on PATH",
    }.get(system, "install the openbao package, or download bao from https://openbao.org/downloads/")
    return (
        "The vault needs Docker or the OpenBao binary, and neither is here. Either install "
        f"Docker and start it, or {how}; then run this again."
    )


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


def _wait_until_active(vault: Any, wait_s: int, sleep: Callable[[float], None]) -> bool:
    """Wait for the server to answer as the active node.

    Raft storage, even with one node, elects itself a moment after unseal. Until it
    has, the server is a standby and answers every request with a 307 to
    ``api_addr``, so the next call failed with ``HTTP Error 307: Temporary
    Redirect`` on a first run that happened to be quick (the onboarding guide's own
    run hit it). ``/v1/sys/health`` answers 200 only once the node is active.
    """
    for _ in range(max(1, wait_s) * 4):
        try:
            vault.call("GET", "/v1/sys/health")
            return True
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 473, 503):  # standby, performance standby, sealed
                raise
        sleep(0.25)
    return False


def _write_config(path: Path, port: int, system: str, run: Runner, notes: list[str]) -> None:
    """Write the container's config once; afterwards only correct a stale ``api_addr``."""
    import re

    want = f'api_addr      = "http://127.0.0.1:{port}"'
    if not path.exists():
        _write_private(path, config_hcl(port), system, run, notes)
        return
    text = path.read_text(encoding="utf-8")
    fixed = re.sub(r'(?m)^api_addr\s*=\s*"[^"]*"\s*$', want, text)
    if fixed != text:
        _write_private(path, fixed, system, run, notes)
        notes.append(f"vault config: api_addr now names port {port}")


class ForeignContainer(SetupError):
    """A container with the vault's name exists and keeps another home's vault."""


def _container_state_dirs(run: Runner, name: str) -> list[Path]:
    """Host folders the container mounts (bind sources), resolved. Empty when it cannot say."""
    done = run(
        ["docker", "inspect", "-f", "{{range .Mounts}}{{.Source}}\n{{end}}", name],
        capture_output=True,
        text=True,
    )
    if getattr(done, "returncode", 1) != 0:
        return []
    out = []
    for line in (done.stdout or "").splitlines():
        if line.strip():
            out.append(Path(os.path.realpath(line.strip())))
    return out


def belongs_to_this_home(run: Runner, name: str, d: Path) -> bool:
    """Whether container *name* keeps its state in *d* (this home's ``<state>/openbao``).

    Decided by what it mounts: a container whose bind mounts all lie elsewhere, or that cannot
    say what it mounts, is somebody else's.
    """
    mine = Path(os.path.realpath(d))
    return any(src == mine or mine in src.parents for src in _container_state_dirs(run, name))


def _refuse_foreign(run: Runner, name: str, d: Path, port: int) -> None:
    sources = _container_state_dirs(run, name)
    where = f" (its state is in {sources[0].parent})" if sources else ""
    raise ForeignContainer(
        f"A container named {name!r} already exists and keeps another home's vault{where}; "
        f"it was left alone. To give this home its own vault, choose another name and port: "
        f"{CONTAINER_ENV}=axi-openbao-2 {PORT_ENV}={port + 10} (then run setup again). "
        f"This home's vault would keep its state in {d}."
    )


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
    runtime: str = "docker",
    which: Callable[[str], str | None] | None = None,
    popen: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Start the vault, initialise and unseal it, issue the kv-only token, record where it is.

    ``runtime`` is ``docker`` (the default), ``binary`` (run ``bao server`` directly, as this
    user, for a machine without Docker) or ``auto`` (Docker when it is usable, else the binary).
    Returns ``{addr, dir, token_file, settings_file, actions, notes, runtime}``. Raises
    :class:`SetupError` with a sentence for the person when neither can run or the server does
    not come up.
    """
    system = system or system_name()
    if runtime == "auto":
        if check_docker(system, which=which, run=run) is None:
            runtime = "docker"
        elif bao_binary(which):
            runtime = "binary"
        else:
            raise SetupError(binary_hint(system))
    if runtime == "docker":
        problem = check_docker(system, which=which, run=run)
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
    _write_config(d / "config.hcl", port, system, run, notes)

    vault = vault_factory(addr)
    if runtime == "binary":
        _start_binary(
            d, port=port, system=system, vault=vault, which=which, popen=popen,
            actions=actions, notes=notes, run=run,
        )
    else:
        _start_container(d, container=container, port=port, system=system, run=run,
                         actions=actions)

    for _ in range(wait_s):
        if vault.reachable():
            break
        sleep(1)
    else:
        if runtime == "binary":
            log = d / "bao.log"
            tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]) \
                if log.exists() else "(no log written)"
            raise SetupError(
                "The vault did not start. Its last log lines:\n" + tail
                + f"\nThe full log is {log}. Fix what it says and run this again."
            )
        logs = run(["docker", "logs", container], capture_output=True, text=True)
        tail = "\n".join(((logs.stdout or "") + (logs.stderr or "")).splitlines()[-8:])
        raise SetupError(
            "The vault did not start. Its last log lines:\n"
            + tail
            + f"\nRemove it (docker rm -f -v {container}), update, and run this again."
        )
    return _initialise(d, addr=addr, vault=vault, system=system, run=run, actions=actions,
                       notes=notes, write_settings=write_settings, runtime=runtime,
                       wait_s=wait_s, sleep=sleep, container=container)


def _start_container(d: Path, *, container: str, port: int, system: str, run: Runner,
                     actions: list[str]) -> None:
    """Start (or create) the vault's container."""
    # Never remove or replace a container this home's setup did not create: a container of
    # that name whose mounts are not this home's state is another home's vault.
    existing = container in _containers(run)
    if existing and not belongs_to_this_home(run, container, d):
        _refuse_foreign(run, container, d, port)

    # A vault that never finished its first start holds nothing yet: start it clean.
    if not (d / "init.json").exists():
        if existing:
            run(["docker", "rm", "-f", "-v", container], capture_output=True, text=True)
            existing = False
        for sub in ("data", "unseal"):
            shutil.rmtree(d / sub, ignore_errors=True)
            (d / sub).mkdir(parents=True, exist_ok=True)

    if existing and not _mounts_unseal(run, container):
        run(["docker", "rm", "-f", "-v", container], capture_output=True, text=True)
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
            hint = ""
            if "allocated" in last or "address already in use" in last:
                hint = (f" Something else already uses port {port}; choose another with "
                        f"{PORT_ENV}={port + 10} and run setup again.")
            raise SetupError(f"Docker could not start the vault: {last}{hint}")
    else:
        run(["docker", "start", container], capture_output=True, text=True)


def _start_binary(d: Path, *, port: int, system: str, vault: Any, which, popen, actions: list[str],
                  notes: list[str], run: Runner) -> None:
    """Run ``bao server`` as this user, detached, unless this home's vault already answers here."""
    if vault.reachable():
        if (d / "init.json").exists():
            return
        try:
            initialised = bool(vault.call("GET", "/v1/sys/init").get("initialized"))
        except Exception:  # noqa: BLE001 - answers, but not as a vault we can read
            initialised = True
        if initialised:
            raise SetupError(
                f"Another vault already answers on port {port} and it is not this home's "
                f"(this home has no record of initialising it); it was left alone. Choose another "
                f"port: {PORT_ENV}={port + 10}, then run setup again."
            )
        return
    exe = bao_binary(which)
    if not exe:
        raise SetupError(binary_hint(system))
    data = d / "data"
    if not (d / "init.json").exists():
        # A vault that never finished its first start holds nothing yet: start it clean.
        shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True, exist_ok=True)
    restrict_to_owner(data, system, run=run, directory=True)
    cfg = d / "server.hcl"
    _write_private(
        cfg,
        BINARY_CONFIG_HCL.format(data=data.as_posix(), port=port, cluster_port=port + 1),
        system, run, notes,
    )
    log = open(d / "bao.log", "ab")  # noqa: SIM115 - handed to the child, which outlives us
    kwargs: dict[str, Any] = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL}
    if system == "windows":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = (popen or subprocess.Popen)([exe, "server", f"-config={cfg}"], **kwargs)
    log.close()
    (d / "bao.pid").write_text(str(getattr(proc, "pid", "")), encoding="utf-8")
    actions.append(f"vault server started from {exe} as your user (log: {d / 'bao.log'})")


def _initialise(d: Path, *, addr: str, vault: Any, system: str, run: Runner, actions: list[str],
                notes: list[str], write_settings, runtime: str, wait_s: int,
                sleep: Callable[[float], None], container: str) -> dict[str, Any]:
    """Initialise (once), unseal, mount kv, issue the kv-only token, record the settings."""
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
    if not _wait_until_active(vault, wait_s, sleep):
        raise SetupError(
            "The vault unsealed but did not become active. Run this again; if it repeats, "
            f"remove it (docker rm -f -v {container}) and run this again."
        )
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
        "runtime": runtime,
    }


__all__ = [
    "BINARY_CONFIG_HCL",
    "CONFIG_HCL",
    "CONTAINER_ENV",
    "ForeignContainer",
    "PORT_ENV",
    "START_SH",
    "SetupError",
    "Vault",
    "bao_binary",
    "belongs_to_this_home",
    "binary_hint",
    "check_docker",
    "config_hcl",
    "docker_hint",
    "resolve_target",
    "restrict_to_owner",
    "setup",
    "user_args",
]
