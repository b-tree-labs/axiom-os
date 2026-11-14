# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Turn Tab completion on: detect the shell, ask, add a marked block, prove it works.

This is one setup step with the same four parts as every other (the ``setup``
walk's ``check`` / ``plan`` / ``apply``, ADR-056): ``check`` only reads, so
``doctor`` runs exactly it and changes nothing; ``plan`` says in one sentence
what ``apply`` would do; ``apply`` writes, then ends with ``check``, so it never
reports success it did not observe.

What it writes is small on purpose. The start-up file gets three marked lines
that source a script kept in the user state directory; the script itself is
argcomplete's, generated for this CLI's name (``neut`` or ``axi``). So the
start-up file never holds sixty lines of shell nobody recognises, re-running
setup regenerates the script without touching the start-up file, and removing
the block removes everything.

**Verified, not assumed.** ``check`` passes only when both halves answer: the
shell, started the way a new terminal starts it, has a completer registered for
the command; and the command itself answers the completion protocol with its
nouns. Either half failing is a red line with one remedy. When this shell cannot
complete at all (``sh``, ``cmd.exe``), the remedy names a terminal and shell for
this machine that can.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Shells argcomplete can register a completer in.
SUPPORTED = ("zsh", "bash", "fish", "pwsh", "powershell")

_LABELS = {
    "zsh": "zsh", "bash": "bash", "fish": "fish", "pwsh": "PowerShell",
    "powershell": "Windows PowerShell", "sh": "sh", "nu": "nushell", "cmd": "cmd.exe",
}

_BEGIN_RE = re.compile(r"^# >>> \S+ tab completion \(added by \S+ setup\) >>>[ \t\r]*$", re.M)
_END_RE = re.compile(r"^# <<< \S+ tab completion <<<[ \t\r]*$", re.M)


def markers(cli: str) -> tuple[str, str]:
    return (f"# >>> {cli} tab completion (added by {cli} setup) >>>", f"# <<< {cli} tab completion <<<")


# --------------------------------------------------------------------------- the step's world


@dataclass(frozen=True)
class Line:
    """One checklist line, the same shape the ``setup`` walk prints."""

    step: str
    title: str
    ok: bool
    status: str
    fix: str = ""
    skipped: bool = False

    @property
    def green(self) -> bool:
        return self.ok or self.skipped

    def as_doctor_row(self) -> dict[str, Any]:
        return {"name": f"Setup: {self.title}", "ok": self.green, "status": self.status,
                "fix": None if self.green else (self.fix or None)}

    def as_dict(self) -> dict[str, Any]:
        return {"step": self.step, "title": self.title, "ok": self.ok, "skipped": self.skipped,
                "status": self.status, "fix": self.fix}


@dataclass
class CompletionEnv:
    """The parts of the setup environment this step reads. A setup run's own env fits too."""

    home: Path
    state_dir: Path
    system: str
    cli: str = "axi"
    say: Any = print
    seams: dict[str, Any] = field(default_factory=dict)


def default_env(params: dict[str, Any] | None = None) -> CompletionEnv:
    params = dict(params or {})
    from axiom.extensions.builtins.secrets.shell_profiles import system_name
    from axiom.infra.paths import get_platform_state_dir

    try:
        from axiom.infra.branding import get_branding

        cli = (get_branding().cli_name or "axi").strip()
    except Exception:  # noqa: BLE001
        cli = "axi"
    return CompletionEnv(
        home=Path(params.get("home") or Path.home()),
        # The platform state dir, the one the setup walk uses, so `setup`,
        # `completion setup` and `doctor` all read one script (named per CLI).
        state_dir=Path(params.get("state_dir") or get_platform_state_dir()),
        system=str(params.get("system") or system_name()),
        cli=str(params.get("cli") or cli),
        say=params.get("say") or (lambda _m: None),
        seams=dict(params.get("seams") or {}),
    )


# --------------------------------------------------------------------------- detection


def detect_shell(env: CompletionEnv) -> str | None:
    """The shell new terminals start: the login shell, else the first one found."""
    forced = env.seams.get("shell")
    if forced:
        return str(forced)
    from axiom.extensions.builtins.secrets import shell_profiles as sp

    environ = env.seams.get("environ")
    found = sp.detect(
        home=env.home, env=environ, system=env.system,
        which=env.seams.get("which") or shutil.which, run=env.seams.get("run", subprocess.run),
    )
    # `detect` lists shells in a fixed order (zsh first). New terminals start the
    # login shell, so that is the one to wire when it is among them.
    login = Path(str((environ if environ is not None else os.environ).get("SHELL") or "")).name
    login = login.lower().removesuffix(".exe")
    if login in found:
        return login
    return found[0] if found else None


def startup_file(shell: str, env: CompletionEnv) -> Path | None:
    """Where the marked block goes for ``shell``, or None when it cannot hold one."""
    h = env.home
    if shell == "zsh":
        return h / ".zshrc"
    if shell == "bash":
        # macOS terminals start login shells, which read ~/.bash_profile; Linux ones read ~/.bashrc.
        return h / (".bashrc" if env.system == "linux" else ".bash_profile")
    if shell == "fish":
        return h / ".config" / "fish" / "conf.d" / f"{env.cli}-completion.fish"
    if shell in ("pwsh", "powershell"):
        from axiom.extensions.builtins.secrets import shell_profiles as sp

        if shell == "powershell" and env.system != "windows":
            return None
        return sp.profile_path(shell, home=h, system=env.system, run=env.seams.get("run"))
    return None


def script_path(shell: str, env: CompletionEnv) -> Path:
    ext = {"zsh": "zsh", "bash": "bash", "fish": "fish"}.get(shell, "ps1")
    return env.state_dir / "completion" / f"{env.cli}.{ext}"


def script_for(shell: str, cli: str) -> str:
    """argcomplete's registration script for ``cli`` in ``shell``."""
    import argcomplete

    kind = "powershell" if shell in ("pwsh", "powershell") else shell
    return argcomplete.shellcode([cli], shell=kind)


def _posix(path: Path, home: Path) -> str:
    try:
        return "$HOME/" + str(path.relative_to(home)).replace("\\", "/")
    except ValueError:
        return str(path)


def block_for(shell: str, env: CompletionEnv) -> str:
    """The marked lines for the start-up file: source the script if it is there."""
    begin, end = markers(env.cli)
    script = script_path(shell, env)
    if shell == "zsh":
        body = [
            "(( $+functions[compdef] )) || { autoload -Uz compinit && compinit; }",
            f'[ -f "{_posix(script, env.home)}" ] && source "{_posix(script, env.home)}"',
        ]
    elif shell == "bash":
        body = [f'[ -f "{_posix(script, env.home)}" ] && source "{_posix(script, env.home)}"']
    elif shell == "fish":
        p = _posix(script, env.home)
        body = [f'test -f "{p}"; and source "{p}"']
    elif shell in ("pwsh", "powershell"):
        q = "'" + str(script).replace("'", "''") + "'"
        body = [f"if (Test-Path -LiteralPath {q}) {{ . {q} }}"]
    else:
        raise ValueError(f"{shell} cannot hold a completion block")
    return "\n".join([begin, *body, end]) + "\n"


# --------------------------------------------------------------------------- terminals


def recommend_terminal(env: CompletionEnv, shell: str | None) -> str:
    """One sentence naming a terminal and shell on this machine that complete."""
    environ = env.seams.get("environ") or os.environ
    term = str(environ.get("TERM_PROGRAM") or "")
    if term == "vscode":
        return ("VS Code's integrated terminal completes already: set its default profile to zsh or bash "
                "(Terminal: Select Default Profile), then run setup again in a new terminal")
    if env.system == "darwin":
        return ("use iTerm2, Ghostty or the built-in Terminal with zsh (the macOS default shell): "
                "`chsh -s /bin/zsh`, open a new window, and run setup again")
    if env.system == "windows":
        return ("use Windows Terminal with WSL (Ubuntu), install there, and run setup again; "
                "PowerShell 7 in Windows Terminal also completes")
    return ("use your distribution's terminal with zsh or fish (`sudo apt install zsh` or "
            "`sudo dnf install zsh`, then `chsh -s $(which zsh)`), open a new terminal, and run setup again")


# --------------------------------------------------------------------------- verification


def _run(env: CompletionEnv, argv: list[str], *, timeout: float = 20.0, extra_env: dict[str, str] | None = None):
    runner = env.seams.get("run", subprocess.run)
    # A completion this process may itself be answering must not leak into the probe.
    run_env = {k: v for k, v in (env.seams.get("environ") or os.environ).items()
               if not (k.startswith("_ARGCOMPLETE") or k.startswith("COMP_"))}
    run_env["HOME"] = str(env.home)
    run_env.update(extra_env or {})
    return runner(argv, capture_output=True, text=True, timeout=timeout, env=run_env, stdin=subprocess.DEVNULL)


def protocol_answers(env: CompletionEnv) -> tuple[bool, str]:
    """Does ``<cli>`` answer argcomplete's protocol with its nouns? ``(ok, detail)``."""
    which = env.seams.get("which") or shutil.which
    exe = which(env.cli)
    if not exe:
        return False, f"`{env.cli}` is not on this shell's PATH"
    fd, out = tempfile.mkstemp(prefix="completion-probe-")
    os.close(fd)
    line = f"{env.cli} "
    try:
        _run(env, [exe], timeout=30.0, extra_env={
            "_ARGCOMPLETE": "1", "_ARGCOMPLETE_IFS": "\n", "COMP_LINE": line,
            "COMP_POINT": str(len(line)), "_ARGCOMPLETE_STDOUT_FILENAME": out,
            "AXI_COMPLETION_NO_REFRESH": "1",
        })
        words = [w for w in Path(out).read_text(encoding="utf-8", errors="replace").split() if w]
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"`{env.cli}` did not answer a completion ({type(exc).__name__})"
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    if not words:
        return False, f"`{env.cli}` answered a completion with nothing"
    return True, f"`{env.cli} <Tab>` offers {len(words)} commands"


def shell_registered(shell: str, env: CompletionEnv) -> tuple[bool, str]:
    """Is a completer registered for ``<cli>`` in a new ``shell``? ``(ok, detail)``."""
    which = env.seams.get("which") or shutil.which
    exe = which(shell) or (which("pwsh") if shell == "powershell" else None)
    if not exe:
        return False, f"{_LABELS.get(shell, shell)} is not installed where setup can start it"
    cli = env.cli
    if shell == "zsh":
        argv = [exe, "-i", "-c", f"print -r -- ${{+_comps[{cli}]}}"]
        ok = lambda done: done.stdout.strip().endswith("1")  # noqa: E731
    elif shell == "bash":
        flags = "-lic" if env.system != "linux" else "-ic"
        argv = [exe, flags, f"complete -p {cli}"]
        ok = lambda done: done.returncode == 0 and cli in done.stdout  # noqa: E731
    elif shell == "fish":
        argv = [exe, "-c", f"complete -C'{cli} '"]
        ok = lambda done: bool(done.stdout.strip())  # noqa: E731
    else:
        argv = [exe, "-NoLogo", "-Command",
                f"(TabExpansion2 -inputScript '{cli} ' -cursorColumn {len(cli) + 1}).CompletionMatches.Count"]
        ok = lambda done: done.stdout.strip().isdigit() and int(done.stdout.strip()) > 0  # noqa: E731
    try:
        done = _run(env, argv, timeout=20.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"a new {_LABELS.get(shell, shell)} could not be started ({type(exc).__name__})"
    if ok(done):
        return True, f"new {_LABELS.get(shell, shell)} terminals complete `{cli}`"
    return False, f"new {_LABELS.get(shell, shell)} terminals do not complete `{cli}` yet"


# --------------------------------------------------------------------------- the step


def _tilde(path: Path, home: Path) -> str:
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


class CompletionStep:
    """``setup`` step ``completion``: Tab completes nouns, verbs, flags and the site's values."""

    name = "completion"
    title = "Tab completion"
    needs: tuple[str, ...] = ()

    def ok(self, status: str, *, skipped: bool = False) -> Line:
        return Line(self.name, self.title, True, status, skipped=skipped)

    def red(self, status: str, fix: str) -> Line:
        return Line(self.name, self.title, False, status, fix)

    def skip(self, status: str) -> Line:
        return Line(self.name, self.title, False, status, skipped=True)

    def _target(self, env) -> tuple[str | None, Path | None]:
        shell = detect_shell(env)
        if shell not in SUPPORTED:
            return shell, None
        return shell, startup_file(shell, env)

    def check(self, env) -> Line:
        shell, path = self._target(env)
        label = _LABELS.get(shell or "", shell or "this shell")
        if shell is None or path is None:
            return self.red(f"{label} cannot complete `{env.cli}`", recommend_terminal(env, shell))
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        from axiom.extensions.builtins.secrets import shell_profiles as sp

        span = sp._find_block(text, _BEGIN_RE, _END_RE)
        if span is None or span[1] == -1 or not script_path(shell, env).exists():
            return self.red(f"Tab does not complete `{env.cli}` in {label} yet",
                            f"run `{env.cli} completion setup` and answer yes to add the marked lines to "
                            f"{_tilde(path, env.home)}")
        answered, detail = protocol_answers(env)
        if not answered:
            return self.red(detail, f"reinstall {env.cli} so its own command answers, then run "
                                    f"`{env.cli} completion check`")
        registered, rdetail = shell_registered(shell, env)
        if not registered:
            return self.red(rdetail + f" ({_tilde(path, env.home)} has the block)",
                            recommend_terminal(env, shell))
        return self.ok(f"{rdetail}; {detail}")

    def plan(self, env) -> str | None:
        shell, path = self._target(env)
        if shell is None or path is None:
            return None
        return (f"turn on Tab completion for `{env.cli}` in {_LABELS.get(shell, shell)}: three marked lines "
                f"in {_tilde(path, env.home)} that load {_tilde(script_path(shell, env), env.home)} "
                "(a backup is made first)")

    def apply(self, env) -> Line:
        from axiom.extensions.builtins.secrets import shell_profiles as sp

        shell, path = self._target(env)
        if shell is None or path is None:
            return self.check(env)
        script = script_path(shell, env)
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(script_for(shell, env.cli), encoding="utf-8")
        done = sp.apply(shell, path, block_for(shell, env), begin_re=_BEGIN_RE, end_re=_END_RE,
                        backup_tag="completion-setup-bak")
        if done.outcome == "refused":
            return self.red(f"{_tilde(path, env.home)} was not changed", done.detail)
        if done.backup:
            env.say(f"    previous {path.name} saved as {done.backup.name}")
        # Fill the value cache now, in the background, so the first Tab has the site's values.
        try:
            from axiom.cli.completion_values import start_background_refresh

            if not env.seams.get("no_refresh"):
                start_background_refresh()
        except Exception:  # noqa: BLE001
            pass
        return self.check(env)


def setup_step():
    """The step in the shape the ``setup`` walk takes.

    When the ``setup`` extension is installed this is a subclass of its ``Step``
    and returns its ``Line``, so the walk, its checklist and ``doctor`` treat it
    like every other step. Without it the step stands alone with the same shape.
    """
    try:
        from axiom.extensions.builtins.setup import steps as setup_steps
    except Exception:  # noqa: BLE001 - the standalone step is the same step
        return CompletionStep()

    class _Step(setup_steps.Step, CompletionStep):  # type: ignore[misc]
        name = CompletionStep.name
        title = CompletionStep.title
        needs = ()

        def ok(self, status: str, *, skipped: bool = False):
            return setup_steps.Line(self.name, self.title, True, status, skipped=skipped)

        def red(self, status: str, fix: str):
            return setup_steps.Line(self.name, self.title, False, status, fix)

        def skip(self, status: str):
            return setup_steps.Line(self.name, self.title, False, status, skipped=True)

        check = CompletionStep.check
        plan = CompletionStep.plan
        apply = CompletionStep.apply

    return _Step()


__all__ = [
    "CompletionEnv",
    "CompletionStep",
    "Line",
    "SUPPORTED",
    "block_for",
    "default_env",
    "detect_shell",
    "markers",
    "protocol_answers",
    "recommend_terminal",
    "script_for",
    "script_path",
    "setup_step",
    "shell_registered",
    "startup_file",
]
