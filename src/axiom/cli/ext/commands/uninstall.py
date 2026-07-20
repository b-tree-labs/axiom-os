# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext uninstall <name>`` — inverse of install.

Flow:

1. Read the install record. Exit 1 if missing.
2. Run ``<venv>/bin/pip uninstall -y <name>`` (skipped with ``--no-pip``
   or ``AXIOM_INSTALL_NO_PIP=1``).
3. Remove the unpacked install directory, refusing to remove anything
   outside ``$AXIOM_HOME/extensions/`` (path-traversal guard).
4. Drop the install-state record.

Pip errors are surfaced as warnings but do **not** block the state/disk
cleanup: if ``pip uninstall`` fails we still leave the user with a clean
install-state so ``axi ext list`` stops showing a phantom record. The
pip warning is printed so the user can investigate.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from axiom.cli.ext._output import console
from axiom.cli.ext.commands.install import extensions_root
from axiom.cli.ext.install_state import drop_install, get_installed
from axiom.cli.ext.provider import CliContext


def _pip_uninstall(name: str, *, announce) -> tuple[int, str]:
    pip_bin = Path(sys.executable).parent / "pip"
    cmd = [str(pip_bin), "uninstall", "-y", name]
    announce(f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, f"pip invocation failed: {exc}"
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _path_under(parent: Path, child: Path) -> bool:
    """True when ``child`` resolves inside ``parent``."""
    try:
        c = child.resolve()
        p = parent.resolve()
    except OSError:
        return False
    return str(c).startswith(str(p) + os.sep) or c == p


def uninstall_extension(
    name: str,
    *,
    no_pip: bool = False,
    announce=None,
) -> str:
    """Remove the extension named ``name``.

    Returns the uninstalled version. Raises :class:`RuntimeError` when
    ``name`` isn't installed or when the path-traversal guard rejects
    the recorded install path.
    """
    announce = announce or (lambda msg: None)
    rec = get_installed(name)
    if rec is None:
        raise RuntimeError(
            f"{name} is not installed; run `axi ext list` to see what is."
        )

    # Path traversal guard. Reject any install_path that escapes
    # $AXIOM_HOME/extensions/.
    ext_root = extensions_root()
    target = Path(rec.install_path)
    if not _path_under(ext_root, target):
        raise RuntimeError(
            f"refusing to remove install_path {rec.install_path!r}: "
            f"resolved path is outside {ext_root} (state file may be corrupt)."
        )

    # pip uninstall — log but do not fail the call on non-zero.
    if no_pip or os.environ.get("AXIOM_INSTALL_NO_PIP"):
        announce("pip: skipped (AXIOM_INSTALL_NO_PIP or --no-pip)")
    else:
        rc, output = _pip_uninstall(name, announce=announce)
        if rc != 0:
            announce(
                f"warning: pip uninstall exited {rc}; continuing with "
                f"state + disk cleanup. Output (tail):\n{output[-500:]}"
            )

    # Remove the unpacked dir (idempotent — may already be gone).
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    drop_install(name)
    return rec.version


class UninstallProvider:
    """Built-in provider for ``axi ext uninstall <name>``."""

    verb = "uninstall"
    description = "Uninstall an axi-managed extension"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("name", help="Extension name to uninstall")
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt (accepted for future-proofing)",
        )
        parser.add_argument(
            "--no-pip",
            action="store_true",
            help=argparse.SUPPRESS,
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        def _announce(msg: str) -> None:
            print(msg)

        try:
            version = uninstall_extension(
                args.name,
                no_pip=args.no_pip,
                announce=_announce,
            )
        except RuntimeError as exc:
            print(f"axi ext uninstall: {exc}")
            return 1

        console().print(f"Uninstalled {args.name} {version}")
        return 0


__all__ = ["UninstallProvider", "uninstall_extension"]
