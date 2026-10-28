# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Where this machine's vault is, kept in the platform's own config rather than in a shell.

The vault was found only through three environment variables (``AXIOM_FOREIGN_SECRETS_BACKEND``,
``AXIOM_OPENBAO_URL``, ``AXIOM_OPENBAO_TOKEN``) that a shell profile exports. A program started
outside a terminal, which is how a desktop AI app or an editor starts an MCP server, never runs that
profile. It found no vault, sent no key, and the node answered 403 "does not hold the medallion"
for a person who had done every step right.

So the settings live in a file every entry point reads when the environment is silent:
``<platform state dir>/config/secrets.toml`` (``~/.axi/config/secrets.toml``, the same under every
product name, so ``axi``, a product's CLI and an MCP server all read one file). It holds the backend,
the URL and the mount, and the token **only as the path of a file** readable by its owner alone. The
token itself is never written here, and never into an MCP client's config file.

The environment still wins, variable by variable: an operator who exports a value has chosen it.

``secrets.toml``::

    [vault]
    backend = "openbao"

    [openbao]
    url = "http://127.0.0.1:8200"
    mount = "kv"
    token_file = "/home/you/.axi/openbao/axi-token"   # 0600, never the token itself
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SETTINGS_FILENAME = "secrets.toml"

ENV_BACKEND = "AXIOM_FOREIGN_SECRETS_BACKEND"
ENV_OPENBAO_URL = "AXIOM_OPENBAO_URL"
ENV_OPENBAO_TOKEN = "AXIOM_OPENBAO_TOKEN"
ENV_OPENBAO_MOUNT = "AXIOM_OPENBAO_MOUNT"

DEFAULT_OPENBAO_URL = "http://localhost:8200"
DEFAULT_OPENBAO_MOUNT = "kv"


class VaultSettingsError(ValueError):
    """Settings that cannot be written as asked (a token file others can read, for one)."""


def settings_path() -> Path:
    """``<platform state dir>/config/secrets.toml``: one file whichever command name started us."""
    from axiom.infra.paths import get_platform_state_dir

    return get_platform_state_dir() / "config" / SETTINGS_FILENAME


def load() -> dict[str, Any]:
    """The settings file as a dict; empty when absent or unreadable. Never raises."""
    path = settings_path()
    try:
        from axiom.infra.toml_compat import tomllib

        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - an unreadable file is no settings, said once in the log
        log.warning("vault settings %s could not be read: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def _owner_only(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return not (mode & (stat.S_IRWXG | stat.S_IRWXO))


def foreign_backend() -> str:
    """The backend that holds stored credentials: the environment, else the settings file, else ''."""
    env = (os.environ.get(ENV_BACKEND) or "").strip().lower()
    if env:
        return env
    return str(_section(load(), "vault").get("backend") or "").strip().lower()


def openbao_url() -> str:
    env = os.environ.get(ENV_OPENBAO_URL)
    if env:
        return env
    return str(_section(load(), "openbao").get("url") or DEFAULT_OPENBAO_URL)


def openbao_mount() -> str:
    env = os.environ.get(ENV_OPENBAO_MOUNT)
    if env:
        return env
    return str(_section(load(), "openbao").get("mount") or DEFAULT_OPENBAO_MOUNT)


def openbao_token() -> str:
    """The OpenBao token: the environment, else the owner-only file the settings name, else ''.

    A token file that anyone but its owner can read is refused, not used: reading it would make the
    settings file a way to spread the token.
    """
    env = os.environ.get(ENV_OPENBAO_TOKEN)
    if env:
        return env
    named = str(_section(load(), "openbao").get("token_file") or "").strip()
    if not named:
        return ""
    path = Path(named).expanduser()
    if not _owner_only(path):
        log.warning("vault token file %s is missing or readable by others; not used", path)
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.warning("vault token file %s could not be read: %s", path, exc)
        return ""


@dataclass(frozen=True)
class Resolution:
    """Where each vault setting came from: ``env``, ``config`` or ``default``/``none``. No values."""

    backend: str
    backend_from: str
    url_from: str
    token_from: str
    token_file: str
    settings_file: str


def describe() -> Resolution:
    """How the vault settings resolve in this process. Never a token value."""
    data = load()
    vault, bao = _section(data, "vault"), _section(data, "openbao")
    if os.environ.get(ENV_BACKEND):
        backend_from = "env"
    elif vault.get("backend"):
        backend_from = "config"
    else:
        backend_from = "none"
    url_from = "env" if os.environ.get(ENV_OPENBAO_URL) else ("config" if bao.get("url") else "default")
    if os.environ.get(ENV_OPENBAO_TOKEN):
        token_from = "env"
    elif bao.get("token_file"):
        token_from = "config" if openbao_token() else "config (file missing or readable by others)"
    else:
        token_from = "none"
    return Resolution(
        backend=foreign_backend(),
        backend_from=backend_from,
        url_from=url_from,
        token_from=token_from,
        token_file=str(bao.get("token_file") or ""),
        settings_file=str(settings_path()),
    )


def write(
    *,
    backend: str,
    url: str = "",
    token_file: str = "",
    mount: str = "",
) -> Path:
    """Record where the vault is. The token is named by its file, which must be readable by its owner only.

    Returns the settings file. Raises :class:`VaultSettingsError` for a token file that does not
    exist or that others can read, and for a value that looks like a token rather than a path.
    """
    backend = (backend or "").strip().lower()
    if not backend:
        raise VaultSettingsError("a backend is required (for example openbao)")
    lines = [
        "# Where this machine's vault is. Written by the secrets command; read by every entry point",
        "# (CLI, MCP server, chat) when the environment does not say. Never holds a secret: the",
        "# token is named by a file readable only by its owner.",
        "",
        "[vault]",
        f"backend = {_toml_str(backend)}",
    ]
    if backend == "openbao":
        lines += ["", "[openbao]", f"url = {_toml_str(url or DEFAULT_OPENBAO_URL)}"]
        lines.append(f"mount = {_toml_str(mount or DEFAULT_OPENBAO_MOUNT)}")
        if token_file:
            path = Path(token_file).expanduser()
            if not path.is_file():
                # Not echoed: a value that is not a file may be the token itself.
                raise VaultSettingsError(
                    "the token file given is not a file: name the file that holds the token, "
                    "never the token"
                )
            if not _owner_only(path):
                raise VaultSettingsError(
                    f"{path} can be read by others; make it owner-only first (chmod 600 {path})"
                )
            lines.append(f"token_file = {_toml_str(str(path.resolve()))}")
    target = settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".toml.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(tmp, target)
    return target


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


__all__ = [
    "ENV_BACKEND",
    "ENV_OPENBAO_MOUNT",
    "ENV_OPENBAO_TOKEN",
    "ENV_OPENBAO_URL",
    "Resolution",
    "SETTINGS_FILENAME",
    "VaultSettingsError",
    "describe",
    "foreign_backend",
    "load",
    "openbao_mount",
    "openbao_token",
    "openbao_url",
    "settings_path",
    "write",
]
