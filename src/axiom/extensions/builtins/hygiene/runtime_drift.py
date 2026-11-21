# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What code, and what environment, is this node actually running?

Three drifts that each ran for days before anyone could see them:

- **Editable runtime off its release (#673).** ``axiom`` resolves through an
  editable install whose checkout is on a feature branch, dirty, or behind the
  newest release tag. Every surface on the node (CLI, MCP server, agent
  daemons) silently runs that checkout. An editable install is allowed (a
  source-canary node keeps one on purpose); it must sit exactly at the newest
  release tag. A node whose posture is the formal install path
  (``node.install_path = "formal"``) is told about any editable install at all.
- **Service runtime differs from the CLI runtime (#703).** An installer wrote a
  script that called a verb the service's own venv did not have; the service
  ran another checkout. Installers record the runtime they target
  (:mod:`axiom.infra.runtime_record`); this check re-probes each one and names
  the mismatch, and says "could not be measured" rather than guessing when the
  probe fails.
- **Serving child missing keys from its EnvironmentFile (#703).** A long-lived
  ``axiom.infra.switch`` supervisor hands each new copy the environment it had
  at boot, so a key added to the unit's EnvironmentFile never reaches the
  serving copy until a full restart. The check compares key NAMES only, never
  values, and names the restart that fixes it.

Every ``check_*`` is pure over collected facts; the ``collect_*`` functions are
the only code that touches git, the interpreter, /proc or systemd, and none of
them raises.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from axiom.extensions.builtins.hygiene.node_health import Finding, Severity
from axiom.infra.runtime_record import RuntimeIdentity

Run = Callable[..., tuple[int, str]]

__all__ = [
    "EditableRuntime",
    "RuntimeIdentity",
    "ServingEnv",
    "check_editable_runtime",
    "check_runtime_parity",
    "check_serving_env_drift",
    "collect_editable_runtime",
    "collect_runtime_facts",
    "editable_target_from_direct_url",
    "env_file_keys",
    "runtime_findings",
]


# ---------------------------------------------------------------- facts


@dataclass(frozen=True)
class EditableRuntime:
    """Where an editable install points, and where that checkout stands."""

    target: Path
    branch: str | None  # None when HEAD is detached
    exact_tag: str | None  # the tag HEAD sits exactly on, if any
    dirty: bool  # tracked files modified
    newest_release_tag: str | None
    contains_newest: bool  # HEAD includes the newest release tag


@dataclass(frozen=True)
class ServingEnv:
    """One supervised unit: its EnvironmentFile's keys and its serving child's keys.

    ``None`` on either side means that side could not be read (permissions, no
    child yet), which is not drift.
    """

    unit: str
    env_file: str
    file_keys: frozenset[str] | set[str] | None
    child_pid: int | None
    child_keys: frozenset[str] | set[str] | None


# ---------------------------------------------------------------- #673


def editable_target_from_direct_url(raw: str) -> Path | None:
    """The checkout an editable install points at, from its ``direct_url.json``."""
    try:
        doc = json.loads(raw or "")
    except ValueError:
        return None
    if not isinstance(doc, dict) or not (doc.get("dir_info") or {}).get("editable"):
        return None
    url = urlparse(str(doc.get("url", "")))
    if url.scheme != "file" or not url.path:
        return None
    return Path(unquote(url.path))


def _installed_editable_target() -> Path | None:
    import importlib.metadata as md

    from axiom.infra.branding import get_branding

    try:
        package = get_branding().package_name or "axiom-os-lm"
        return editable_target_from_direct_url(
            md.distribution(package).read_text("direct_url.json") or ""
        )
    except Exception:  # noqa: BLE001 - no install record is "not editable"
        return None


def collect_editable_runtime(target: Path, *, run: Run) -> EditableRuntime:
    """Ask git where the editable checkout stands. Local refs only: no fetch."""

    def git(*args: str) -> tuple[int, str]:
        return run(["git", "-C", str(target), *args])

    rc, branch = git("symbolic-ref", "-q", "--short", "HEAD")
    rc_tag, tag = git("describe", "--tags", "--exact-match", "HEAD")
    _, status = git("status", "--porcelain", "--untracked-files=no")
    _, tags = git("tag", "--list", "v*", "--sort=-v:refname")
    newest = next((t.strip() for t in tags.splitlines() if t.strip()), None)
    contains = True
    if newest:
        rc_anc, _ = git("merge-base", "--is-ancestor", newest, "HEAD")
        contains = rc_anc == 0
    return EditableRuntime(
        target=Path(target),
        branch=branch.strip() if rc == 0 and branch.strip() else None,
        exact_tag=tag.strip() if rc_tag == 0 and tag.strip() else None,
        dirty=bool(status.strip()),
        newest_release_tag=newest,
        contains_newest=contains,
    )


def check_editable_runtime(
    facts: EditableRuntime | None, *, formal_install_path: bool = False
) -> list[Finding]:
    """WARNING naming each way the editable checkout is off its release."""
    if facts is None:
        return []
    if formal_install_path:
        return [
            Finding(
                check="editable_install_policy",
                severity=Severity.INFO,
                message=(
                    f"This node's install path is formal, but the package is an "
                    f"editable install of {facts.target}. Reinstall from the "
                    f"release wheel."
                ),
                current_value=f"editable: {facts.target}",
                expected_value="installed from a release",
            )
        ]
    problems: list[str] = []
    if facts.exact_tag is None:
        where = f"on branch {facts.branch}" if facts.branch else "detached away from any tag"
        problems.append(f"not at a release tag ({where})")
    if facts.dirty:
        problems.append("has uncommitted changes")
    if facts.newest_release_tag and not facts.contains_newest:
        problems.append(f"is behind the newest release tag {facts.newest_release_tag}")
    if not problems:
        return []
    goal = facts.newest_release_tag or "the newest release tag"
    return [
        Finding(
            check="editable_runtime",
            severity=Severity.WARNING,
            message=(
                f"Every surface on this node runs the editable checkout {facts.target}, "
                f"which is {'; '.join(problems)}. Move it to {goal} "
                f"(git -C {facts.target} checkout {goal}) or install the release."
            ),
            current_value=facts.exact_tag or facts.branch or "detached",
            expected_value=goal,
        )
    ]


# ---------------------------------------------------------------- #703 parity


def check_runtime_parity(
    records: dict[str, RuntimeIdentity], current: RuntimeIdentity | None
) -> list[Finding]:
    """A service whose runtime is not the CLI's, or could not be measured."""
    if current is None:
        return []
    findings: list[Finding] = []
    for label, svc in sorted(records.items()):
        if svc.error:
            findings.append(
                Finding(
                    check="runtime_parity",
                    severity=Severity.WARNING,
                    message=(
                        f"The runtime {label} was installed against ({svc.interpreter}) "
                        f"could not be measured: {svc.error}. The service is likely "
                        f"failing; reinstall it from this CLI."
                    ),
                    current_value=svc.interpreter,
                    expected_value=current.interpreter,
                )
            )
        elif not svc.same_as(current):
            findings.append(
                Finding(
                    check="runtime_parity",
                    severity=Severity.WARNING,
                    message=(
                        f"{label} runs {svc.version or '?'} from {svc.package_root or '?'} "
                        f"({svc.interpreter}); the CLI runs {current.version} from "
                        f"{current.package_root}. A verb one has and the other lacks "
                        f"fails only in the service. Reinstall it from this CLI."
                    ),
                    current_value=f"{svc.version} @ {svc.package_root}",
                    expected_value=f"{current.version} @ {current.package_root}",
                )
            )
    return findings


# ---------------------------------------------------------------- #703 env keys

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def env_file_keys(text: str) -> set[str]:
    """The variable NAMES an EnvironmentFile sets. Values are never kept."""
    keys: set[str] = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if m:
            keys.add(m.group(1))
    return keys


def check_serving_env_drift(units: Iterable[ServingEnv]) -> list[Finding]:
    """Keys in the unit's EnvironmentFile that its serving child never received."""
    findings: list[Finding] = []
    for u in units:
        if u.file_keys is None or u.child_keys is None:
            continue
        missing = sorted(set(u.file_keys) - set(u.child_keys))
        if not missing:
            continue
        findings.append(
            Finding(
                check="serving_env_drift",
                severity=Severity.WARNING,
                message=(
                    f"{u.unit}: the serving copy (pid {u.child_pid}) lacks "
                    f"{len(missing)} key(s) its EnvironmentFile {u.env_file} sets: "
                    f"{', '.join(missing)}. A switch hands each new copy the "
                    f"supervisor's boot environment, so an env-file change needs "
                    f"`systemctl restart {u.unit}` (or run the supervisor with "
                    f"--env-file so a switch re-reads it)."
                ),
                current_value=", ".join(missing),
                expected_value="every EnvironmentFile key present in the serving copy",
            )
        )
    return findings


# ---------------------------------------------------------------- collectors


def _current_runtime() -> RuntimeIdentity:
    import sys

    from axiom.infra.runtime_record import identity_of_this_process

    try:
        return identity_of_this_process()
    except Exception as exc:  # noqa: BLE001
        return RuntimeIdentity(interpreter=sys.executable, error=str(exc))


def _formal_install_path() -> bool:
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        return str(SettingsStore().get("node.install_path") or "").lower() == "formal"
    except Exception:  # noqa: BLE001 - no settings: no declared posture
        return False


def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _child_pids(pid: int) -> list[int]:
    text = _read(Path(f"/proc/{pid}/task/{pid}/children")) or ""
    return [int(p) for p in text.split() if p.isdigit()]


def _environ_keys(pid: int) -> set[str] | None:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    return {
        chunk.split(b"=", 1)[0].decode(errors="replace")
        for chunk in raw.split(b"\0")
        if b"=" in chunk
    }


def _parse_show(blocks: str) -> list[dict[str, str]]:
    units: list[dict[str, str]] = []
    for block in blocks.split("\n\n"):
        props: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition("=")
            props[key] = value
        if props.get("Id"):
            units.append(props)
    return units


def collect_serving_env(run: Run) -> list[ServingEnv]:
    """Supervised units (ExecStart runs ``axiom.infra.switch``) on this host."""
    out: list[ServingEnv] = []
    for scope in ([], ["--user"]):
        rc, listing = run(
            ["systemctl", *scope, "list-units", "--type=service", "--state=running",
             "--no-legend", "--plain"]
        )
        names = [ln.split()[0] for ln in listing.splitlines() if rc == 0 and ln.split()]
        if not names:
            continue
        rc, shown = run(
            ["systemctl", *scope, "show", *names, "-p", "Id", "-p", "MainPID",
             "-p", "ExecStart", "-p", "EnvironmentFiles"]
        )
        if rc != 0:
            continue
        for props in _parse_show(shown):
            if "axiom.infra.switch" not in props.get("ExecStart", ""):
                continue
            env_files = [
                tok for tok in props.get("EnvironmentFiles", "").split()
                if tok.startswith("/")
            ]
            pid = int(props.get("MainPID") or 0)
            children = _child_pids(pid) if pid else []
            child = children[-1] if children else None
            child_keys = _environ_keys(child) if child else None
            for env_file in env_files:
                text = _read(Path(env_file))
                out.append(
                    ServingEnv(
                        unit=props["Id"],
                        env_file=env_file,
                        file_keys=env_file_keys(text) if text is not None else None,
                        child_pid=child,
                        child_keys=child_keys,
                    )
                )
    return out


def collect_runtime_facts(run: Run) -> dict:
    """Every fact the three checks need, collected without raising."""
    import sys

    facts: dict = {
        "editable": None,
        "formal_install_path": _formal_install_path(),
        "records": {},
        "current": _current_runtime(),
        "serving_env": [],
    }
    try:
        target = _installed_editable_target()
        if target is not None and target.is_dir():
            facts["editable"] = collect_editable_runtime(target, run=run)
    except Exception:  # noqa: BLE001
        pass
    try:
        from axiom.infra.runtime_record import probe_recorded_runtimes

        facts["records"] = probe_recorded_runtimes()
    except Exception:  # noqa: BLE001
        pass
    if sys.platform.startswith("linux"):
        try:
            facts["serving_env"] = collect_serving_env(run)
        except Exception:  # noqa: BLE001
            pass
    return facts


def runtime_findings(facts: dict) -> list[Finding]:
    """All three checks over one set of collected facts."""
    return [
        *check_editable_runtime(
            facts.get("editable"), formal_install_path=bool(facts.get("formal_install_path"))
        ),
        *check_runtime_parity(facts.get("records") or {}, facts.get("current")),
        *check_serving_env_drift(facts.get("serving_env") or []),
    ]
