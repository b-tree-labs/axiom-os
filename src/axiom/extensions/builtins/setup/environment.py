# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What this computer is: detected once, read by every setup step.

Detection only reads. It never installs, starts or edits anything, which is what
lets ``doctor`` run the same checks as ``setup`` without changing the machine.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE_FILENAME = "setup-state.json"


def cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:  # noqa: BLE001
        return "axi"


def product_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().product_name or "Axiom"
    except Exception:  # noqa: BLE001
        return "Axiom"


def system_name() -> str:
    from axiom.extensions.builtins.secrets.shell_profiles import system_name as _sys

    return _sys()


def is_wsl(env: dict[str, str] | None = None, proc_version: Path = Path("/proc/version")) -> bool:
    """Windows Subsystem for Linux: Linux, with a Windows browser and Windows paths beside it."""
    env = os.environ if env is None else env
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in proc_version.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def installer(prefix: Path | None = None, env: dict[str, str] | None = None) -> str:
    """How this install was made: ``checkout``, ``uv tool``, ``pipx``, ``conda``, ``venv`` or ``pip``."""
    from axiom.extensions.builtins.update.installer import _is_pipx_env, _is_uv_tool_env
    from axiom.extensions.builtins.update.offer import _running_from_a_checkout

    env = os.environ if env is None else env
    pfx = Path(prefix or sys.prefix)
    if prefix is None and _running_from_a_checkout():
        return "checkout"
    if _is_uv_tool_env(pfx):
        return "uv tool"
    if _is_pipx_env(pfx):
        return "pipx"
    if (pfx / "conda-meta").is_dir() or env.get("CONDA_PREFIX") == str(pfx):
        return "conda"
    if sys.base_prefix != sys.prefix:
        return "venv"
    return "pip"


@dataclass
class SetupEnv:
    """Everything a step needs to know, resolved once per run.

    ``home`` and ``state_dir`` are fields, not lookups, so a test runs every step
    against a temporary home and never the real ``~/.axi``.
    """

    home: Path
    state_dir: Path
    system: str
    wsl: bool = False
    node: str = ""
    site: str = ""
    cli: str = "axi"
    product: str = "Axiom"
    timeout_s: float = 5.0
    #: Long enough for a first vault start, which may pull an image.
    vault_wait_s: int = 180
    #: Seconds the browser sign-in waits for the person.
    signin_timeout_s: float = 300.0
    say: Callable[[str], None] = print
    #: Seams for the parts that reach outside the process. ``None`` uses the real one.
    seams: dict[str, Any] = field(default_factory=dict)

    def seam(self, name: str, default: Any) -> Any:
        return self.seams.get(name, default)

    @property
    def state_file(self) -> Path:
        return self.state_dir / STATE_FILENAME


def load_state(state_dir: Path) -> dict[str, Any]:
    """The last run's record: the node and site it used and what each step ended as."""
    try:
        data = json.loads((state_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state_dir: Path, *, node: str, site: str, lines: list[Any]) -> None:
    from axiom.infra.state import atomic_write

    atomic_write(
        state_dir / STATE_FILENAME,
        {
            "node": node,
            "site": site,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "steps": {ln.step: {"ok": ln.ok, "status": ln.status} for ln in lines},
        },
    )


def brand_defaults() -> dict[str, str]:
    """``{"node", "site"}`` the product says this machine should use, if it knows.

    A consumer layer answers from its own configuration (a site binding, a site
    file); the platform never knows an organisation's node. Never raises.
    """
    try:
        from axiom.infra.branding import get_branding

        hook = getattr(get_branding(), "setup_defaults_fn", None)
        out = hook() if callable(hook) else None
    except Exception:  # noqa: BLE001 - a brand must not break setup
        return {}
    if not isinstance(out, dict):
        return {}
    return {k: str(out.get(k) or "").strip() for k in ("node", "site")}


def resolve_env(params: dict[str, Any], *, state_dir: Path | None = None) -> SetupEnv:
    """Build the environment from a skill's params, then the brand, then the last run.

    Precedence for the node address: what the person typed, then
    ``AXIOM_NODE_URL``, then the product's default (its site binding or site
    file), then whatever the last run used.
    """
    from axiom.infra.paths import get_platform_state_dir

    home = Path(params.get("home") or Path.home())
    sdir = Path(params.get("state_dir") or state_dir or get_platform_state_dir())
    last = load_state(sdir)
    brand = brand_defaults()
    node = (
        str(params.get("node") or "").strip()
        or os.environ.get("AXIOM_NODE_URL", "").strip()
        or brand.get("node", "")
        or str(last.get("node") or "")
    ).rstrip("/")
    site = (
        str(params.get("site") or "").strip() or brand.get("site", "") or str(last.get("site") or "")
    )
    return SetupEnv(
        home=home,
        state_dir=sdir,
        system=str(params.get("system") or system_name()),
        wsl=bool(params.get("wsl")) if "wsl" in params else is_wsl(),
        node=node,
        site=site,
        cli=cli_name(),
        product=product_name(),
        timeout_s=float(params.get("timeout_s") or 5.0),
        say=params.get("say") or print,
        seams=dict(params.get("seams") or {}),
    )


__all__ = [
    "STATE_FILENAME",
    "SetupEnv",
    "brand_defaults",
    "cli_name",
    "installer",
    "is_wsl",
    "load_state",
    "product_name",
    "resolve_env",
    "save_state",
    "system_name",
]
