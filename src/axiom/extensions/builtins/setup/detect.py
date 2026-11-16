# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One paragraph about this computer, printed before setup asks anything.

Reads only. Each fact is guarded: a detector that fails reports "unknown" rather
than stopping setup, because the steps re-check what they need anyway.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from .environment import SetupEnv, installer
from .steps import _tilde

_OS = {"darwin": "macOS", "linux": "Linux", "windows": "Windows"}


def describe(env: SetupEnv) -> dict[str, Any]:
    from axiom.extensions.builtins.secrets import shell_profiles as sp
    from axiom.extensions.builtins.secrets import vault_setup

    out: dict[str, Any] = {"os": _OS.get(env.system, env.system) + (" (WSL)" if env.wsl else "")}
    which = env.seams.get("which") or shutil.which
    try:
        shells = sp.detect(home=env.home, env=env.seams.get("environ"), system=env.system,
                           which=which, run=env.seams.get("run", subprocess.run))
    except Exception:  # noqa: BLE001
        shells = []
    if shells:
        path = sp.profile_path(shells[0], home=env.home, system=env.system, run=None)
        out["shell"] = sp.LABELS[shells[0]]
        out["startup_file"] = _tilde(path, env.home) if path else ""
    else:
        out["shell"], out["startup_file"] = "unknown", ""
    try:
        out["installer"] = env.seams.get("installer") or installer()
    except Exception:  # noqa: BLE001
        out["installer"] = "unknown"
    try:
        problem = vault_setup.check_docker(env.system, which=which,
                                           run=env.seams.get("run", subprocess.run))
        out["docker"] = "running" if problem is None else (
            "not installed" if not which("docker") else "installed, not running")
    except Exception:  # noqa: BLE001
        out["docker"] = "unknown"
    out["openbao_binary"] = bool(vault_setup.bao_binary(which))
    try:
        from axiom.extensions.builtins.mcp import install as mcp

        detected = env.seams.get("detect_tools", mcp.detect_tools)()
        out["ai_apps"] = sorted(t for t, present in detected.items() if present)
    except Exception:  # noqa: BLE001
        out["ai_apps"] = []
    out["node"] = env.node
    return out


def summary(found: dict[str, Any]) -> str:
    from .steps import _APP_LABELS

    parts = [found.get("os", "")]
    shell = found.get("shell") or "unknown shell"
    sf = found.get("startup_file") or ""
    parts.append(f"{shell}" + (f" ({sf})" if sf else ""))
    how = found.get("installer", "unknown")
    parts.append("a source checkout" if how == "checkout" else f"installed with {how}")
    parts.append(f"Docker {found.get('docker', 'unknown')}")
    if found.get("docker") != "running" and found.get("openbao_binary"):
        parts.append("OpenBao program found")
    apps = [_APP_LABELS.get(a, a) for a in found.get("ai_apps") or []]
    parts.append("AI apps: " + (", ".join(apps) if apps else "none found"))
    return " · ".join(p for p in parts if p)


__all__ = ["describe", "summary"]
