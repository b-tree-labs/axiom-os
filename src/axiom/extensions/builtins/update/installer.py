# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Pick the upgrade command that matches how the running interpreter was installed.

``update`` used to run ``sys.executable -m pip`` unconditionally. A
``uv tool install`` environment ships no pip, so the upgrade died with
"No module named pip" and aborted. The tool that installed the package is the
tool that must upgrade it:

  uv tool   -> ``uv tool upgrade <dist>`` (uv replays the receipt, extras included)
  pipx      -> ``pipx upgrade <dist>``
  pip venv  -> ``python -m pip install -U <dist>[extras]``
  no pip, uv on PATH -> ``uv pip install --python <exe> -U <dist>[extras]``
  anything else -> no command is run; the operator is told what to run.
"""

from __future__ import annotations

import importlib.metadata as md
import importlib.util
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstallPlan:
    """How to upgrade: ``cmd`` is None when we must not (or cannot) run anything."""

    method: str  # "uv-tool" | "pipx" | "pip" | "uv-pip" | "unknown"
    cmd: list[str] | None
    manual: str  # the one command a human should run (or the reason, if none)

    @property
    def label(self) -> str:
        return {
            "uv-tool": "uv tool",
            "pipx": "pipx",
            "pip": "pip",
            "uv-pip": "uv pip",
        }.get(self.method, self.method)


def _receipt_extras(prefix: Path, dist: str) -> list[str]:
    """Extras recorded in a uv tool receipt (``uv-receipt.toml``)."""
    receipt = prefix / "uv-receipt.toml"
    try:
        import tomllib

        data = tomllib.loads(receipt.read_text())
    except Exception:  # noqa: BLE001
        return []
    want = dist.lower().replace("_", "-")
    for req in data.get("tool", {}).get("requirements", []):
        if str(req.get("name", "")).lower().replace("_", "-") == want:
            return sorted(str(e) for e in req.get("extras", []) or [])
    return []


def installed_extras(dist: str, prefix: Path | None = None) -> list[str]:
    """Extras the user installed with, best effort.

    A uv receipt records them exactly. Otherwise a pip install leaves no
    record, so an extra counts as installed when it has requirements and every
    one of them is present in this environment.
    """
    if prefix is not None and (prefix / "uv-receipt.toml").is_file():
        return _receipt_extras(prefix, dist)
    try:
        meta = md.metadata(dist)
        from packaging.requirements import Requirement
    except Exception:  # noqa: BLE001
        return []
    by_extra: dict[str, list[str]] = {}
    for line in md.requires(dist) or []:
        try:
            req = Requirement(line)
        except Exception:  # noqa: BLE001
            continue
        if req.marker is None:
            continue
        for extra in meta.get_all("Provides-Extra") or []:
            # Applicable on this platform AND tied to this extra.
            if req.marker.evaluate({"extra": extra}) and not req.marker.evaluate(
                {"extra": ""}
            ):
                by_extra.setdefault(extra, []).append(req.name)
    found = []
    for extra, names in by_extra.items():
        if extra == "all":
            continue
        ok = True
        for n in names:
            try:
                md.version(n)
            except md.PackageNotFoundError:
                ok = False
                break
        if ok and names:
            found.append(extra)
    return sorted(found)


def _spec(dist: str, extras: list[str]) -> str:
    return f"{dist}[{','.join(extras)}]" if extras else dist


def _is_uv_tool_env(prefix: Path) -> bool:
    if (prefix / "uv-receipt.toml").is_file():
        return True
    parts = [p.lower() for p in prefix.parts]
    return "uv" in parts and "tools" in parts


def _is_pipx_env(prefix: Path) -> bool:
    if (prefix / "pipx_metadata.json").is_file():
        return True
    parts = [p.lower() for p in prefix.parts]
    return "pipx" in parts and "venvs" in parts


def _has_pip() -> bool:
    return importlib.util.find_spec("pip") is not None


def plan_upgrade(
    dist: str,
    *,
    source_spec: str | None = None,
    executable: str | None = None,
    prefix: str | Path | None = None,
    has_pip: bool | None = None,
    extras: list[str] | None = None,
) -> InstallPlan:
    """Choose the upgrade command for an installed (non-editable) distribution.

    ``source_spec`` is a full requirement (e.g. ``name @ git+https://...``) for
    products distributed from a repo instead of an index; extras are applied
    to its name.
    """
    exe = executable or sys.executable
    pfx = Path(prefix) if prefix is not None else Path(sys.prefix)
    pip_ok = _has_pip() if has_pip is None else has_pip
    ex = installed_extras(dist, pfx) if extras is None else extras

    if source_spec:
        target = source_spec
        if ex:
            target = source_spec.replace(dist, _spec(dist, ex), 1)
    else:
        target = _spec(dist, ex)

    if _is_uv_tool_env(pfx):
        if source_spec:
            cmd = ["uv", "tool", "install", "--force", target]
        else:
            cmd = ["uv", "tool", "upgrade", dist]
        return _plan("uv-tool", cmd, tool="uv")

    if _is_pipx_env(pfx):
        if source_spec:
            cmd = ["pipx", "install", "--force", target]
        else:
            cmd = ["pipx", "upgrade", dist]
        return _plan("pipx", cmd, tool="pipx")

    if pip_ok:
        cmd = [exe, "-m", "pip", "install", "--upgrade", target, "-q"]
        return InstallPlan("pip", cmd, _shell(cmd))

    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--python", exe, "--upgrade", target, "-q"]
        return InstallPlan("uv-pip", cmd, _shell(cmd))

    manual = f"uv pip install --python {exe} --upgrade '{target}'"
    return InstallPlan("unknown", None, manual)


def _plan(method: str, cmd: list[str], *, tool: str) -> InstallPlan:
    """The tool that installed it may not be on PATH here; then, just tell them."""
    if shutil.which(tool) is None:
        return InstallPlan(method, None, _shell(cmd))
    return InstallPlan(method, cmd, _shell(cmd))


def _shell(cmd: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(c) for c in cmd)
