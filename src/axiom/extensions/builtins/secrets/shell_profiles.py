# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Point a person's shells at the local vault, in whichever shells they actually use.

Profiles are a convenience, not the mechanism: every entry point finds the vault from
``secrets.toml`` and the token *file* it names (:mod:`vault_settings`), so an app started outside
a terminal works with no profile at all. What a profile adds is the three variables for tools that
only read the environment.

One block, one set of markers, in every shell's own syntax. The block is found by its markers, so
running setup again updates it in place and never stacks a second copy, and a copy written by the
older ``start-local-vault.sh`` is recognised as the same block.

**The token is never a value in a profile.** The block reads it from the token file when the shell
starts. It is likewise never put in the registry or the environment of a Windows account: cmd.exe
has no profile to read a file from, so it is offered the URL and backend only, and told to use
PowerShell or rely on ``secrets.toml`` for the token.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

#: The marker text the shell-script version wrote, with the product's name in it. A block carrying
#: any product's name is the same block, so one written by the script is updated, not duplicated.
_BEGIN_RE = re.compile(r"^# >>> .+? vault \(added by start-local-vault\.sh\) >>>[ \t\r]*$", re.M)
_END_RE = re.compile(r"^# <<< .+? vault <<<[ \t\r]*$", re.M)


def product_name() -> str:
    """The active product's display name (``Axiom``, or the consumer layer's brand)."""
    try:
        from axiom.infra.branding import get_branding

        return get_branding().product_name
    except Exception:  # noqa: BLE001 - a label is not worth failing setup over
        return "Axiom"


def markers(product: str | None = None) -> tuple[str, str]:
    """``(begin, end)`` marker lines, named for the product."""
    product = product or product_name()
    return (
        f"# >>> {product} vault (added by start-local-vault.sh) >>>",
        f"# <<< {product} vault <<<",
    )


ENV_BACKEND = "AXIOM_FOREIGN_SECRETS_BACKEND"
ENV_URL = "AXIOM_OPENBAO_URL"
ENV_TOKEN = "AXIOM_OPENBAO_TOKEN"

#: Shell keys, in the order they are offered.
SHELLS = ("zsh", "bash", "sh", "fish", "pwsh", "powershell", "nu", "cmd")

LABELS = {
    "zsh": "zsh",
    "bash": "bash",
    "sh": "sh / dash / ksh",
    "fish": "fish",
    "pwsh": "PowerShell 7+",
    "powershell": "Windows PowerShell 5",
    "nu": "nushell",
    "cmd": "Windows cmd.exe",
}

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def system_name() -> str:
    """``darwin``, ``linux`` or ``windows``."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


# ---------------------------------------------------------------- rendering


_SAFE = re.compile(r"[\w./:@%+=~\-]+")


def _posix_path(path: str) -> str:
    p = path.replace("\\", "/")
    return p if _SAFE.fullmatch(p) else '"' + p.replace('"', '\\"') + '"'


def _ps_quote(path: str) -> str:
    return "'" + path.replace("'", "''") + "'"


def render(shell: str, *, url: str, token_file: str, product: str | None = None) -> str:
    """The marked block for ``shell``. Reads the token from ``token_file``; never holds it."""
    if shell in ("zsh", "bash", "sh"):
        body = [
            f"export {ENV_BACKEND}=openbao",
            f"export {ENV_URL}={url}",
            f'export {ENV_TOKEN}="$(cat {_posix_path(token_file)})"',
        ]
    elif shell == "fish":
        body = [
            f"set -gx {ENV_BACKEND} openbao",
            f"set -gx {ENV_URL} {url}",
            f"set -gx {ENV_TOKEN} (cat {_posix_path(token_file)})",
        ]
    elif shell in ("pwsh", "powershell"):
        q = _ps_quote(token_file)
        body = [
            f'$env:{ENV_BACKEND} = "openbao"',
            f'$env:{ENV_URL} = "{url}"',
            f"if (Test-Path -LiteralPath {q}) {{ $env:{ENV_TOKEN} = (Get-Content -Raw -LiteralPath {q}).Trim() }}",
        ]
    elif shell == "nu":
        body = [
            f'$env.{ENV_BACKEND} = "openbao"',
            f'$env.{ENV_URL} = "{url}"',
            f'$env.{ENV_TOKEN} = (open --raw "{token_file}" | str trim)',
        ]
    else:
        raise ValueError(f"no profile syntax for {shell!r}")
    begin, end = markers(product)
    return "\n".join([begin, *body, end]) + "\n"


# ---------------------------------------------------------------- locating profiles


def _ps_profile(exe: str, home: Path, system: str, run: Runner | None) -> Path | None:
    """Ask PowerShell itself where its all-hosts profile is (Documents may be redirected)."""
    if run is not None and shutil.which(exe):
        try:
            done = run(
                [exe, "-NoProfile", "-Command", "$PROFILE.CurrentUserAllHosts"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            line = (done.stdout or "").strip().splitlines()
            if done.returncode == 0 and line:
                return Path(line[-1].strip())
        except (OSError, subprocess.SubprocessError):
            pass
    if system == "windows":
        folder = "PowerShell" if exe == "pwsh" else "WindowsPowerShell"
        return home / "Documents" / folder / "profile.ps1"
    return home / ".config" / "powershell" / "profile.ps1"


def profile_path(
    shell: str, *, home: Path, system: str | None = None, run: Runner | None = subprocess.run
) -> Path | None:
    """Where ``shell`` reads its start-up from, or ``None`` when it has no profile (cmd.exe)."""
    system = system or system_name()
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "bash":
        # Git Bash on Windows reads ~/.bash_profile, like a macOS login shell.
        return home / (".bashrc" if system == "linux" else ".bash_profile")
    if shell == "sh":
        return home / ".profile"
    if shell == "fish":
        return home / ".config" / "fish" / "conf.d" / f"{product_name().lower()}-vault.fish"
    if shell in ("pwsh", "powershell"):
        if shell == "powershell" and system != "windows":
            return None
        return _ps_profile(shell, home, system, run)
    if shell == "nu":
        if run is not None and shutil.which("nu"):
            try:
                done = run(["nu", "-c", "$nu.env-path"], capture_output=True, text=True, timeout=20)
                out = (done.stdout or "").strip()
                if done.returncode == 0 and out:
                    return Path(out)
            except (OSError, subprocess.SubprocessError):
                pass
        return home / ".config" / "nushell" / "env.nu"
    return None


# ---------------------------------------------------------------- detection


def detect(
    *,
    home: Path,
    env: Mapping[str, str] | None = None,
    system: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner | None = subprocess.run,
) -> list[str]:
    """The shells this person actually has, the one they are in first.

    A shell counts when it is their login shell (``$SHELL``), the process that started us, an
    installed shell that is not a given on this OS (fish, PowerShell, nushell), or one whose profile
    already exists.
    """
    env = os.environ if env is None else env
    system = system or system_name()
    found: list[str] = []

    def add(name: str | None) -> None:
        if name and name not in found:
            found.append(name)

    def from_exe(raw: str) -> str | None:
        base = Path(raw.strip().lstrip("-")).name.lower().removesuffix(".exe")
        return {
            "zsh": "zsh",
            "bash": "bash",
            "sh": "sh",
            "dash": "sh",
            "ksh": "sh",
            "fish": "fish",
            "pwsh": "pwsh",
            "powershell": "powershell",
            "nu": "nu",
            "cmd": "cmd",
        }.get(base)

    add(from_exe(env.get("SHELL", "")) if env.get("SHELL") else None)
    if run is not None and system != "windows":
        try:
            done = run(
                ["ps", "-p", str(os.getppid()), "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if done.returncode == 0:
                add(from_exe(done.stdout))
        except (OSError, subprocess.SubprocessError):
            pass
    if system == "windows" and not env.get("SHELL"):
        # Not under Git Bash: the console is PowerShell when the module path says so, else cmd.
        add("pwsh" if which("pwsh") else None)
        add("powershell" if which("powershell") else None)
    for name in ("zsh", "bash", "sh"):
        path = profile_path(name, home=home, system=system, run=None)
        if path and path.exists():
            add(name)
    for name in ("fish", "pwsh", "powershell", "nu"):
        if name == "powershell" and system != "windows":
            continue
        path = profile_path(name, home=home, system=system, run=None)
        if which(name) or (path and path.exists()):
            add(name)
    if system == "windows":
        add("cmd")
    return [s for s in SHELLS if s in found]


# ---------------------------------------------------------------- writing


@dataclass
class Applied:
    shell: str
    path: Path | None
    #: ``added`` / ``updated`` / ``unchanged`` / ``refused``
    outcome: str
    backup: Path | None = None
    detail: str = ""
    lines: list[str] = field(default_factory=list)


def _find_block(text: str) -> tuple[int, int, str, str] | None:
    """``(start, stop, begin_line, end_line)`` of the marked block, or ``(start, -1, ...)`` when it
    has a start marker and no end. ``stop`` includes one trailing newline."""
    begin = _BEGIN_RE.search(text)
    if begin is None:
        return None
    end = _END_RE.search(text, begin.end())
    if end is None:
        return (begin.start(), -1, begin.group(0), "")
    stop = end.end()
    if text[stop : stop + 2] == "\r\n":
        stop += 2
    elif text[stop : stop + 1] == "\n":
        stop += 1
    return (begin.start(), stop, begin.group(0), end.group(0))


def apply(shell: str, path: Path, block: str, *, now: datetime | None = None) -> Applied:
    """Write ``block`` into ``path``: replace the marked block if there, else append it.

    Backs the file up beside itself first whenever it is about to change. Refuses, touching
    nothing, when a begin marker has no end marker: guessing where the block stops could eat a
    person's own configuration.
    """
    existing = ""
    if path.exists():
        # newline="" keeps CRLF as written, so an unchanged file compares equal and a Windows
        # profile does not come back with mixed endings.
        with open(path, encoding="utf-8", newline="") as handle:
            existing = handle.read()
    crlf = "\r\n" in existing
    new_block = block.replace("\n", "\r\n") if crlf else block
    span = _find_block(existing)
    if span is not None and span[1] == -1:
        return Applied(
            shell,
            path,
            "refused",
            detail=f"{path} has the start marker but not the end marker; fix or remove "
            f"the line {span[2]!r} and run this again",
        )
    if span is None:
        sep = "" if not existing or existing.endswith("\n") else ("\r\n" if crlf else "\n")
        lead = ("\r\n" if crlf else "\n") if existing else ""
        updated = existing + sep + lead + new_block
        outcome = "added"
    else:
        # Keep the marker lines already on file, so a block written under another product's name
        # (or by the old script) compares equal instead of being rewritten for a label.
        lines = new_block.split("\n")
        eol = "\r" if crlf else ""
        lines[0] = span[2].rstrip("\r") + eol
        for i in range(len(lines) - 1, -1, -1):
            if _END_RE.match(lines[i]):
                lines[i] = span[3].rstrip("\r") + eol
                break
        updated = existing[: span[0]] + "\n".join(lines) + existing[span[1] :]
        outcome = "unchanged" if updated == existing else "updated"
    if updated == existing:
        return Applied(shell, path, "unchanged")
    backup = None
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.vault-setup-bak-{stamp}")
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    os.replace(tmp, path)
    return Applied(shell, path, outcome, backup=backup)


def cmd_setx(url: str, run: Runner = subprocess.run) -> list[str]:
    """Persist the URL and backend (never the token) as the Windows user's variables.

    ``setx`` writes HKCU\\Environment. Returns one line per variable saying what happened.
    """
    out = []
    for name, value in ((ENV_BACKEND, "openbao"), (ENV_URL, url)):
        try:
            done = run(["setx", name, value], capture_output=True, text=True, timeout=20)
            ok = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        out.append(f"{name} {'saved for your Windows account' if ok else 'could not be saved'}")
    return out


CMD_NOTE = (
    "cmd.exe has no start-up file to read a token from, so only the address and backend were "
    "saved as user variables. The token is deliberately never put in the registry or the "
    "environment: use PowerShell (profile written above), or rely on secrets.toml, which "
    "names the token file and is read by apps and the CLI without any variable."
)


__all__ = [
    "SHELLS",
    "LABELS",
    "Applied",
    "CMD_NOTE",
    "apply",
    "cmd_setx",
    "detect",
    "markers",
    "profile_path",
    "render",
    "system_name",
]
