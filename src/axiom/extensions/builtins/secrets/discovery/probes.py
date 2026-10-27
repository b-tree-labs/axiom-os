# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where credential material should not live — a registry of probes.

Each probe knows one location kind and yields :class:`RawHit`s. Values stay
inside this layer: the skill above converts them to fingerprinted findings.

Probes are deliberately dumb about vendors — they surface text, and the matcher
registry decides what is credential-shaped. That is what keeps "a new kind of
place" and "a new kind of token" independent changes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol, runtime_checkable

from axiom.extensions.builtins.secrets.discovery.matchers import match_text
from axiom.extensions.builtins.secrets.discovery.model import RawHit, credential_target


@runtime_checkable
class CredentialProbe(Protocol):
    name: str

    def scan(self, root: Path) -> Iterable[RawHit]: ...


class GitRemoteProbe:
    """Credentials embedded in git remote URLs.

    The worst hiding place in practice: `git remote -v` prints it, it survives
    clones and backups, and no secret store ever sees it.
    """

    name = "git-remote"

    def scan(self, root: Path) -> Iterable[RawHit]:
        for gitdir in sorted(root.glob("*/.git")):
            repo = gitdir.parent
            try:
                url = subprocess.run(
                    ["git", "-C", str(repo), "remote", "get-url", "origin"],
                    capture_output=True, text=True, timeout=15,
                ).stdout.strip()
            except Exception:  # noqa: BLE001 — a probe must never break the sweep
                continue
            if not url:
                continue
            for _matcher, value in match_text(url):
                yield RawHit(
                    locator=f"{repo.name}:remote/origin",
                    value=value,
                    probe=self.name,
                    detail="embedded in a git remote URL",
                    target=credential_target(url),
                )


class GitCredentialsProbe:
    """`~/.git-credentials` — better than a URL, still plaintext on disk."""

    name = "git-credentials"

    def scan(self, root: Path) -> Iterable[RawHit]:
        path = root / ".git-credentials"
        if not path.is_file():
            return
        mode = oct(path.stat().st_mode & 0o777)[2:]
        for line in path.read_text(errors="replace").splitlines():
            for _matcher, value in match_text(line):
                yield RawHit(
                    locator=str(path),
                    value=value,
                    probe=self.name,
                    detail=f"plaintext credential file (mode {mode})",
                )


class EnvFileProbe:
    """Env files and unit drop-ins — where deploy scripts park secrets."""

    name = "env-file"
    #: Recursive by design. The skill roots this at ``~/.config``, but real
    #: deployments park credentials one level down (``~/.config/<app>/x.env``),
    #: so a flat ``*.env`` glob reports a clean sweep over a directory full of
    #: secrets. ``**/`` also matches depth 0, so the flat case still works.
    PATTERNS = ("**/*.env", "**/*.env.local", "**/*.conf")
    #: Bound the walk. Credentials sit near the top; the depth below a scan
    #: root is what separates ``~/.config/axiom/`` from a vendored app tree.
    MAX_DEPTH = 3

    def __init__(
        self, patterns: tuple[str, ...] | None = None, *, max_depth: int | None = None
    ) -> None:
        self.patterns = patterns or self.PATTERNS
        self.max_depth = self.MAX_DEPTH if max_depth is None else max_depth

    def scan(self, root: Path) -> Iterable[RawHit]:
        seen: set[Path] = set()
        for pattern in self.patterns:
            for path in sorted(root.glob(pattern)):
                if path in seen or not path.is_file():
                    continue
                try:
                    if len(path.relative_to(root).parts) > self.max_depth:
                        continue
                except ValueError:  # pragma: no cover — glob result is under root
                    continue
                seen.add(path)
                try:
                    text = path.read_text(errors="replace")
                except OSError:
                    continue
                mode = oct(path.stat().st_mode & 0o777)[2:]
                for line in text.splitlines():
                    if line.lstrip().startswith("#"):
                        continue  # a documented example is not a leak
                    for _matcher, value in match_text(line):
                        key = line.split("=", 1)[0].strip() if "=" in line else "?"
                        yield RawHit(
                            locator=f"{path}:{key}",
                            value=value,
                            probe=self.name,
                            detail=f"env file (mode {mode})",
                            target=credential_target(line),
                        )


PROBES: dict[str, CredentialProbe] = {}


def register_probe(probe: CredentialProbe) -> None:
    PROBES[probe.name] = probe


for _p in (GitRemoteProbe(), GitCredentialsProbe(), EnvFileProbe()):
    register_probe(_p)


def run_probes(
    roots: dict[str, Path],
    *,
    only: Iterable[str] | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
) -> list[RawHit]:
    """Run each probe over its root. A failing probe never aborts the sweep —
    a partial inventory is useful; a crashed one is not."""
    wanted = set(only) if only else set(PROBES)
    hits: list[RawHit] = []
    for name, probe in PROBES.items():
        if name not in wanted:
            continue
        root = roots.get(name) or roots.get("default")
        if root is None:
            continue
        try:
            hits.extend(probe.scan(Path(root)))
        except Exception as exc:  # noqa: BLE001
            if on_error:
                on_error(name, exc)
    return hits


__all__ = [
    "PROBES",
    "CredentialProbe",
    "EnvFileProbe",
    "ProcessEnvProbe",
    "GitCredentialsProbe",
    "GitRemoteProbe",
    "register_probe",
    "run_probes",
]


class ProcessEnvProbe:
    """Credential material held in RUNNING process environments.

    A process environment is a snapshot taken at ``exec``. Rotating a
    credential updates it at rest — the env file, the vault — and changes
    nothing for anything already running. The unit stays ``active``, reports
    healthy, and silently fails every operation that needs the credential.

    That is not hypothetical: a chat backend ran for three weeks holding a
    pre-rotation database password, answering every grounded question with a
    polite apology, while the credential on disk was correct the whole time.

    The FLEET view (every pid) needs ``/proc``, which is Linux. Reporting
    nothing at all on macOS was the gap that mattered: that is the platform
    the people onboarding actually develop on, so the one probe that would
    have caught a key held in a live environment was a no-op exactly where
    it was needed. A key exported from a shell rc reaches this process's own
    environment, and an agent or test runner started from that shell can
    print it into a transcript — which is how one did.

    So where ``/proc`` is absent, this probe still reports THIS process's
    environment. One pid instead of all of them, and that pid is the sweep
    itself — which is honest about its scope and is the inheritance path a
    developer's own credentials actually travel.
    """

    name = "process-env"

    def __init__(self, proc_root: str | Path = "/proc") -> None:
        self._proc = Path(proc_root)

    def _own_environment(self) -> Iterator[RawHit]:
        import os

        for key, value in sorted(os.environ.items()):
            for _matcher, found in match_text(value):
                yield RawHit(
                    locator=f"this process ({key})",
                    value=found,
                    probe=self.name,
                    detail="held in this process's own environment — "
                           "inherited from the shell that started it, and "
                           "by everything it starts in turn",
                    target=credential_target(value),
                )

    def scan(self, root: Path) -> Iterator[RawHit]:  # noqa: ARG002 — root unused
        if not self._proc.is_dir():
            yield from self._own_environment()
            return
        for entry in sorted(self._proc.iterdir()):
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "environ").read_bytes().decode("utf-8", "replace")
                cmd = (entry / "comm").read_text().strip()
            except (OSError, PermissionError):
                continue  # not ours to read; never a reason to abort the sweep
            for item in raw.split("\0"):
                if "=" not in item:
                    continue
                key, _, value = item.partition("=")
                for _matcher, found in match_text(value):
                    yield RawHit(
                        locator=f"pid {entry.name} ({cmd}) {key}",
                        value=found,
                        probe=self.name,
                        detail="held in a running process environment",
                        target=credential_target(value),
                    )


register_probe(ProcessEnvProbe())
class ShellRcProbe:
    """Credentials exported from a shell startup file.

    The place people actually put an API key. `EnvFileProbe` looks for
    ``*.env`` under a config root; a key exported from ``~/.zshrc`` is in
    none of those, is read into every shell the operator opens, and is
    inherited by every process they start from it — which is how one
    reached a test runner's ``os.environ`` and was printed into a
    transcript by a traceback.

    Scanned by NAME rather than by glob: these files are a known, short,
    closed list, and widening it to "dotfiles in $HOME" would read SSH
    config, history files and editor state for no gain.
    """

    name = "shell-rc"
    #: Login and interactive startup files, bash/zsh/fish. Ordered by how
    #: often an export actually lands in one.
    FILENAMES = (
        ".zshrc", ".zshenv", ".zprofile", ".zlogin",
        ".bashrc", ".bash_profile", ".bash_login", ".profile",
        ".config/fish/config.fish",
    )

    def __init__(self, filenames: tuple[str, ...] | None = None) -> None:
        self.filenames = filenames or self.FILENAMES

    def scan(self, root: Path) -> Iterator[RawHit]:
        for name in self.filenames:
            path = root / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            mode = oct(path.stat().st_mode & 0o777)[2:]
            for line in text.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue  # a commented example is not a leak
                for _matcher, value in match_text(line):
                    key = "?"
                    if "=" in line:
                        lhs = line.split("=", 1)[0]
                        key = lhs.replace("export", "").strip()
                    yield RawHit(
                        locator=f"{path}:{key}",
                        value=value,
                        probe=self.name,
                        detail=f"shell startup file (mode {mode}) — "
                               "read into every shell and inherited by "
                               "everything started from it",
                        target=credential_target(line),
                    )


register_probe(ShellRcProbe())

