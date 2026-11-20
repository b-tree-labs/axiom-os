# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The runtime of record for everything Axiom installs (#703).

A launchd job called ``axi memory capture-sweep`` through a service venv that
was an editable install of a checkout parked on a feature branch. The verb
existed on main and in the CLI's venv, not in the service's, and the job
reported ``invalid choice`` as a stale ledger every five minutes for two days.
Nothing had recorded which runtime the service was bound to, and nothing
checked that it could do what the installed script asked of it.

This module gives installers both halves:

- :func:`probe_verb` asks the TARGET runtime whether it has the verb the
  installed script will call, so an installer can refuse instead of writing a
  script that can only fail;
- :func:`record_runtime` writes ``<state>/runtimes/<label>.json`` with the
  interpreter, package version and package root the service targets, and
  :func:`probe_recorded_runtimes` re-measures each one so ``hygiene stat
  health`` can compare it with the CLI's (``runtime_parity``).

Probes run the target's own interpreter, so they see what the service sees,
and they never raise: a runtime that cannot be measured says why.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "RuntimeIdentity",
    "identity_of_this_process",
    "interpreter_of",
    "probe_recorded_runtimes",
    "probe_runtime",
    "probe_verb",
    "record_runtime",
]

Runner = Callable[..., subprocess.CompletedProcess]

#: Run inside the target interpreter; prints that runtime's identity as JSON.
#: The same code computes this process's identity, so the two always compare
#: like with like.
_PROBE = r"""
import importlib.metadata as md, json, sys
from pathlib import Path
import axiom
dists = [*(md.packages_distributions().get("axiom") or []), "axiom-os-lm"]
version = ""
for d in dists:
    try:
        version = md.version(d)
        break
    except Exception:
        pass
print(json.dumps({
    "interpreter": sys.executable,
    "version": version,
    "package_root": str(Path(axiom.__file__).resolve().parent),
}))
"""


@dataclass(frozen=True)
class RuntimeIdentity:
    """The interpreter a surface runs, the package version and where it loads from."""

    interpreter: str = ""
    version: str = ""
    package_root: str = ""
    error: str = ""  # set when the runtime could not be probed

    def same_as(self, other: RuntimeIdentity) -> bool:
        return self.version == other.version and self.package_root == other.package_root


def _run(cmd: Sequence[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd), capture_output=True, text=True, timeout=timeout, check=False
    )


def identity_of_this_process() -> RuntimeIdentity:
    """This process's runtime, computed the way :data:`_PROBE` computes another's.

    The probe is a self-contained string because it must run in a runtime that
    may predate this module; keep the two in step.
    """
    import importlib.metadata as md

    import axiom

    version = ""
    for dist in [*(md.packages_distributions().get("axiom") or []), "axiom-os-lm"]:
        try:
            version = md.version(dist)
            break
        except md.PackageNotFoundError:
            continue
    return RuntimeIdentity(
        interpreter=sys.executable,
        version=version,
        package_root=str(Path(axiom.__file__).resolve().parent),
    )


def interpreter_of(axi_binary: str | Path) -> str:
    """The Python an installed console script runs, from its shebang."""
    try:
        with open(axi_binary, "rb") as fh:
            first = fh.readline(4096).decode(errors="replace").strip()
    except OSError:
        return ""
    if not first.startswith("#!"):
        return ""
    parts = first[2:].split()
    if not parts:
        return ""
    # "#!/usr/bin/env python3" names the interpreter second.
    return parts[1] if parts[0].endswith("/env") and len(parts) > 1 else parts[0]


def probe_runtime(interpreter: str, *, run: Runner = _run) -> RuntimeIdentity:
    """Measure the runtime ``interpreter`` loads. Never raises."""
    if not interpreter:
        return RuntimeIdentity(error="no interpreter recorded")
    try:
        proc = run([interpreter, "-c", _PROBE])
    except (OSError, subprocess.SubprocessError) as exc:
        return RuntimeIdentity(interpreter=interpreter, error=f"{type(exc).__name__}: {exc}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
        return RuntimeIdentity(interpreter=interpreter, error=tail[0][:200])
    try:
        return RuntimeIdentity(**json.loads(proc.stdout.strip().splitlines()[-1]))
    except (ValueError, TypeError, IndexError):
        return RuntimeIdentity(interpreter=interpreter, error="unreadable probe output")


def probe_verb(
    axi_binary: str | Path, verb: Sequence[str], *, run: Runner = _run
) -> tuple[bool, str]:
    """Whether the runtime behind ``axi_binary`` has ``verb`` (``--help`` exits 0).

    An installer calls this before writing a script that invokes the verb, and
    refuses when it is missing: a script whose call contract the target lacks
    can only fail, and its failure is easily mistaken for the condition it
    monitors.
    """
    cmd = [str(axi_binary), *verb, "--help"]
    try:
        proc = run(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{' '.join(cmd)}: {type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, ""
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
    return False, f"{' '.join(cmd)} exited {proc.returncode}: {tail[0][:200]}"


def _records_dir(state_dir: Path | None) -> Path:
    if state_dir is None:
        from axiom.infra.paths import get_user_state_dir

        state_dir = get_user_state_dir()
    return Path(state_dir) / "runtimes"


def record_runtime(
    label: str,
    axi_binary: str | Path,
    *,
    state_dir: Path | None = None,
    run: Runner = _run,
) -> dict:
    """Write the runtime ``label`` targets; returns the record written."""
    interpreter = interpreter_of(axi_binary) or sys.executable
    ident = probe_runtime(interpreter, run=run)
    record = {
        "label": label,
        "axi_binary": str(axi_binary),
        **asdict(ident),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    d = _records_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{label}.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def forget_runtime(label: str, *, state_dir: Path | None = None) -> None:
    """Drop ``label``'s record when its service is uninstalled."""
    try:
        (_records_dir(state_dir) / f"{label}.json").unlink()
    except OSError:
        pass


def probe_recorded_runtimes(
    *, state_dir: Path | None = None, run: Runner = _run
) -> dict[str, RuntimeIdentity]:
    """Re-measure every recorded service runtime now: ``{label: identity}``."""
    d = _records_dir(state_dir)
    out: dict[str, RuntimeIdentity] = {}
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            rec = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        interpreter = rec.get("interpreter") or interpreter_of(rec.get("axi_binary", ""))
        out[str(rec.get("label") or path.stem)] = probe_runtime(interpreter, run=run)
    return out
