# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A red check the pull request did not cause, repaired without a person.

When an upstream release or the base branch moves, every pull request that
runs the check goes red, whatever it changed. These functions decide who
caused a failure, which toolchain CI runs the repair under, whether a
verified repair may be pushed, which blocked pull request carries the
repair while the others wait, and what the owners of the touched paths are
told afterwards.

Every function is pure. The harness supplies the facts (the pull request's
changed files, the base branch's last result, the workflow text, the
repair's diff stats, the verify result) and runs the commands itself.
Nothing here calls a model, the network, or pushes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

import yaml

from axiom.extensions.builtins.release.repair_catalog import ChurnBudget

#: Node's bundled npm major, for when a workflow does not pin npm itself.
NODE_BUNDLED_NPM = {"16": "8", "18": "10", "20": "10", "22": "10", "24": "11"}

#: Paths whose change is never pushed without a person: secrets, keys,
#: auth configuration, ownership, and workflows (which carry permissions).
SENSITIVE_GLOBS = (
    ".github/workflows/**",
    "**/CODEOWNERS",
    "**/.env",
    "**/.env.*",
    "**/*secret*",
    "**/*.pem",
    "**/*.key",
    "**/auth/**",
    "**/*permissions*",
)


# --- globs -----------------------------------------------------------------


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def path_matches(path: str, globs: Iterable[str]) -> bool:
    """Whether ``path`` matches any glob (``*`` stays in a segment, ``**`` crosses)."""
    clean = path.strip().removeprefix("./")
    return any(_glob_regex(glob.strip()).match(clean) for glob in globs if glob.strip())


# --- attribution ---------------------------------------------------------------


def _when(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def attribute_failure(
    *,
    entry_inputs: Iterable[str] = (),
    pr_files: Iterable[str] | None = None,
    check_globs: Iterable[str] | None = None,
    base_result: str = "",
    base_result_at: str = "",
    upstream_release_at: str = "",
    inputs_complete: bool = False,
) -> dict[str, Any]:
    """``caused_by_pr``, ``base_drift`` or ``undetermined``, with the evidence.

    ``entry_inputs`` are the files the failure is about (a manifest, a
    lockfile). ``check_globs`` are the check's inputs. ``base_result`` is the
    base branch's latest result for the same check, and ``base_result_at``
    its time. ``upstream_release_at`` is when an upstream release changed
    what the check compares against, if known. ``inputs_complete`` says the
    failure depends only on ``entry_inputs`` (and the outside world), so a
    pull request that changes none of them leaves the base failing the same
    way. ``next_fact`` names the fact that would decide an undetermined
    verdict.
    """
    inputs = tuple(entry_inputs)
    files = None if pr_files is None else [f for f in pr_files if str(f).strip()]
    globs = None if check_globs is None else [g for g in check_globs if str(g).strip()]
    base = (base_result or "").strip().lower()
    base_at = _when(base_result_at)
    upstream_at = _when(upstream_release_at)
    evidence: list[str] = []

    touched_inputs = [f for f in files or [] if inputs and path_matches(f, inputs)]
    if files is not None and inputs:
        if touched_inputs:
            evidence.append(f"the pull request changes {', '.join(touched_inputs)}")
        else:
            evidence.append("the pull request changes no package or manifest file this failure is about")
    touched_check = None
    if files is not None and globs:
        touched_check = [f for f in files if path_matches(f, globs)]
        if not touched_check:
            evidence.append("the pull request changes none of the check's inputs")

    def verdict(name: str, next_fact: str = "") -> dict[str, Any]:
        return {"verdict": name, "evidence": evidence, "next_fact": next_fact}

    if base in {"failure", "failed", "red"}:
        evidence.append("the same check is red on the base branch")
        return verdict("base_drift")
    if touched_inputs:
        return verdict("caused_by_pr")
    stale_base = (
        base in {"success", "passed", "green"}
        and base_at is not None
        and upstream_at is not None
        and upstream_at > base_at
    )
    if stale_base:
        evidence.append(
            f"the base branch was last green at {base_result_at}, before the upstream "
            f"release at {upstream_release_at}; it would fail the same way now"
        )
        if files is not None:
            return verdict("base_drift")
        return verdict("undetermined", "the pull request's changed files")
    if inputs_complete and files is not None and inputs and not touched_inputs:
        evidence.append(
            "this failure depends only on those files, which the pull request does not "
            "change, so the base branch fails the same way"
        )
        return verdict("base_drift")
    if base in {"success", "passed", "green"}:
        evidence.append("the base branch is green for this check since any known upstream change")
        if touched_check == [] or (files is not None and inputs and not touched_inputs):
            return verdict("undetermined", "re-run the check on the base branch to confirm")
        if touched_check:
            return verdict("caused_by_pr")
    if touched_check == []:
        return verdict("base_drift")
    return verdict(
        "undetermined",
        "the base branch's latest result for this check"
        if not base
        else "the pull request's changed files",
    )


# --- toolchain -------------------------------------------------------------------


def _find_job(doc: Mapping[str, Any], job: str) -> Mapping[str, Any] | None:
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, Mapping) or not jobs:
        return None
    wanted = (job or "").strip()
    if wanted in jobs:
        return jobs[wanted]
    for value in jobs.values():
        if isinstance(value, Mapping) and str(value.get("name") or "").strip() == wanted:
            return value
    if not wanted and len(jobs) == 1:
        return next(iter(jobs.values()))
    return None


def _resolve(value: Any, matrix: Mapping[str, Any]) -> tuple[str, list[str]]:
    text = str(value if value is not None else "").strip()
    found = re.fullmatch(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}", text)
    if not found:
        return text, []
    options = matrix.get(found.group(1)) or []
    options = [str(item) for item in (options if isinstance(options, list) else [options])]
    return (options[0] if options else ""), options


def _major(version: str) -> str:
    found = re.match(r"v?(\d+)", version.strip())
    return found.group(1) if found else ""


def detect_toolchain(workflow: str, job: str = "") -> dict[str, Any]:
    """The runtime CI runs ``job`` under, read from the workflow text.

    The repair is pinned to this, never to the local machine: a lockfile
    regenerated by a different npm major churns thousands of lines and can
    still fail CI's install.
    """
    result: dict[str, Any] = {
        "found": False,
        "node_version": "",
        "npm_version": "",
        "python_version": "",
        "package_manager": "",
        "working_directory": "",
        "image": "",
        "pin": "",
        "notes": [],
    }
    try:
        doc = yaml.safe_load(workflow or "") or {}
    except yaml.YAMLError as exc:
        result["notes"].append(f"workflow did not parse: {exc}")
        return result
    if not isinstance(doc, Mapping):
        return result
    spec = _find_job(doc, job)
    if spec is None:
        if workflow.strip():
            result["notes"].append(f"no job named {job!r} in the workflow")
        return result
    matrix = ((spec.get("strategy") or {}).get("matrix") or {}) if isinstance(spec, Mapping) else {}
    defaults = (
        ((spec.get("defaults") or {}).get("run") or {}).get("working-directory")
        or ((doc.get("defaults") or {}).get("run") or {}).get("working-directory")
        or ""
    )
    result["working_directory"] = str(defaults)
    npm_pin = ""
    for step in spec.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        uses = str(step.get("uses") or "")
        given = step.get("with") or {}
        if uses.startswith("actions/setup-node"):
            version, options = _resolve(given.get("node-version"), matrix)
            if len(options) > 1:
                result["notes"].append(f"node matrix {options}; pinned to the first")
            if not version and given.get("node-version-file"):
                result["notes"].append(f"node version comes from {given['node-version-file']}")
            result["node_version"] = _major(version) or version
            cache = str(given.get("cache") or "")
            if cache in {"npm", "yarn", "pnpm"}:
                result["package_manager"] = cache
        elif uses.startswith("actions/setup-python"):
            version, options = _resolve(given.get("python-version"), matrix)
            if len(options) > 1:
                result["notes"].append(f"python matrix {options}; pinned to the first")
            result["python_version"] = version
        elif uses.startswith("pnpm/action-setup"):
            result["package_manager"] = "pnpm"
        run = str(step.get("run") or "")
        pinned = re.search(r"npm (?:install|i) (?:-g|--global) npm@([\w.\-]+)", run)
        if pinned:
            npm_pin = pinned.group(1)
        if not result["package_manager"]:
            for manager, pattern in (
                ("pnpm", r"\bpnpm (install|i)\b"),
                ("yarn", r"\byarn( install)?\b"),
                ("npm", r"\bnpm (ci|install|i)\b"),
                ("uv", r"\buv (sync|pip)\b"),
                ("poetry", r"\bpoetry install\b"),
                ("pip", r"\bpip install\b"),
            ):
                if re.search(pattern, run):
                    result["package_manager"] = manager
                    break
        if not result["working_directory"] and step.get("working-directory") and run:
            result["working_directory"] = str(step["working-directory"])
    node = result["node_version"]
    if node:
        result["npm_version"] = npm_pin or NODE_BUNDLED_NPM.get(node, "")
        result["image"] = f"node:{node}"
        result["pin"] = f"Node {node}" + (f", npm {result['npm_version']}" if result["npm_version"] else "")
    elif result["python_version"]:
        result["image"] = f"python:{result['python_version']}"
        result["pin"] = f"Python {result['python_version']}"
    result["found"] = bool(node or result["python_version"])
    return result


def workflow_paths(workflow: str) -> list[str]:
    """The ``on.pull_request.paths`` filter of a workflow: the check's inputs."""
    try:
        doc = yaml.safe_load(workflow or "") or {}
    except yaml.YAMLError:
        return []
    if not isinstance(doc, Mapping):
        return []
    trigger = doc.get("on", doc.get(True))
    if not isinstance(trigger, Mapping):
        return []
    event = trigger.get("pull_request") or trigger.get("pull_request_target") or {}
    paths = event.get("paths") if isinstance(event, Mapping) else None
    return [str(item) for item in paths or [] if not str(item).startswith("!")]


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+", text or "")


def choose_execution(
    toolchain: Mapping[str, Any], *, docker: bool, local: Mapping[str, str]
) -> dict[str, str]:
    """``container``, ``local`` or ``escalate``, and why.

    With a container runtime the repair runs in CI's image. Without one it
    runs locally only when the local tools match CI's pinned versions
    (Node and npm majors; Python major and minor). Anything else is a
    reason, never a guess.
    """
    if docker and toolchain.get("image"):
        return {"mode": "container", "reason": f"in {toolchain['image']}"}
    wanted: list[tuple[str, str, str, int]] = []
    if toolchain.get("node_version"):
        wanted.append(("Node", str(toolchain["node_version"]), str(local.get("node") or ""), 1))
        if toolchain.get("npm_version"):
            wanted.append(("npm", str(toolchain["npm_version"]), str(local.get("npm") or ""), 1))
    if toolchain.get("python_version"):
        wanted.append(("Python", str(toolchain["python_version"]), str(local.get("python") or ""), 2))
    if not wanted:
        return {"mode": "escalate", "reason": "CI's toolchain is not known"}
    misses = []
    for label, ci, here, depth in wanted:
        if _numbers(ci)[:depth] != _numbers(here)[:depth] or not _numbers(here):
            have = ".".join(_numbers(here)[:depth]) or "none"
            need = ".".join(_numbers(ci)[:depth])
            misses.append(f"local {label} {have} is not CI's {label} {need}")
    if misses:
        return {
            "mode": "escalate",
            "reason": "no container runtime, and " + "; ".join(misses),
        }
    return {"mode": "local", "reason": "local tools match CI's pinned versions"}


def render_commands(commands: Iterable[str], toolchain: Mapping[str, Any]) -> list[str]:
    """Each command run inside CI's runtime image, in CI's working directory."""
    image = str(toolchain.get("image") or "")
    workdir = str(toolchain.get("working_directory") or "").strip("/")
    node = str(toolchain.get("node_version") or "")
    npm = str(toolchain.get("npm_version") or "")
    bundled = NODE_BUNDLED_NPM.get(node, "")
    pre = f"npm install -g npm@{npm} >/dev/null && " if npm and npm != bundled else ""
    out = []
    for command in commands:
        text = command
        if not image:
            out.append(f"cd {workdir} && {text}" if workdir else text)
            continue
        where = f"/work/{workdir}" if workdir else "/work"
        inner = (pre + text).replace("'", "'\\''")
        out.append(f"docker run --rm -v \"$PWD\":/work -w {where} {image} sh -c '{inner}'")
    return out


# --- what the repair changed -------------------------------------------------------


def parse_numstat(diff_stats: Any) -> dict[str, Any]:
    """Totals from ``git diff --numstat`` text, or from a list of per-file rows."""
    rows: list[tuple[int, int, str]] = []
    if isinstance(diff_stats, str):
        for line in diff_stats.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added = int(parts[0]) if parts[0].isdigit() else 0
            deleted = int(parts[1]) if parts[1].isdigit() else 0
            rows.append((added, deleted, parts[2].strip()))
    elif isinstance(diff_stats, Iterable):
        for item in diff_stats:
            if isinstance(item, Mapping):
                rows.append(
                    (int(item.get("added") or 0), int(item.get("deleted") or 0), str(item.get("path") or ""))
                )
    return {
        "files": len(rows),
        "lines": sum(added + deleted for added, deleted, _ in rows),
        "paths": [path for _, _, path in rows],
    }


def version_changes(log: str) -> list[dict[str, Any]]:
    """``package current -> expected`` pairs a log asks for, each marked major or not.

    A major change is a different leading number, or for a ``0.x`` package a
    different minor, which semver treats as breaking.
    """
    out = []
    pattern = re.compile(
        r"^\s*(@?[\w./-]+)\s+v?(\d[\w.\-]*)\s+-\s+expected version:\s*[~^>=]*\s*(\d[\w.\-]*)",
        re.IGNORECASE | re.MULTILINE,
    )
    for name, current, expected in pattern.findall(log or ""):
        out.append(
            {
                "package": name,
                "current": current,
                "expected": expected,
                "major_change": _is_major(current, expected),
            }
        )
    return out


def _is_major(current: str, expected: str) -> bool:
    left = re.findall(r"\d+", current)
    right = re.findall(r"\d+", expected)
    if not left or not right:
        return False
    if left[0] != right[0]:
        return True
    return left[0] == "0" and len(left) > 1 and len(right) > 1 and left[1] != right[1]


# --- ownership --------------------------------------------------------------------


def _codeowners_regex(pattern: str) -> re.Pattern[str]:
    text = pattern.strip()
    anchored = text.startswith("/") or "/" in text.rstrip("/")
    text = text.lstrip("/")
    directory = text.endswith("/")
    text = text.rstrip("/")
    body = _glob_regex(text).pattern[1:-1]
    prefix = "^" if anchored else "^(?:.*/)?"
    suffix = "/.*$" if directory else "(?:/.*)?$"
    return re.compile(prefix + body + suffix)


def codeowners_for(path: str, codeowners: str) -> list[str]:
    """The owners CODEOWNERS gives ``path``. The last matching rule wins."""
    owners: list[str] = []
    clean = path.strip().removeprefix("./")
    for line in (codeowners or "").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        pattern, *names = stripped.split()
        if _codeowners_regex(pattern).match(clean):
            owners = names
    return owners


def _same(actor: str, owner: str) -> bool:
    return actor.strip().lstrip("@").lower() == owner.strip().lstrip("@").lower()


def owners_to_tell(
    paths: Iterable[str],
    *,
    actor: str = "",
    codeowners: str = "",
    recent_authors: Mapping[str, Iterable[str]] | None = None,
) -> list[str]:
    """Who owns the touched paths, other than the actor. CODEOWNERS first."""
    found: list[str] = []
    for path in paths:
        names = codeowners_for(path, codeowners) if codeowners else []
        if not names and recent_authors:
            names = list(recent_authors.get(path) or [])
        for name in names:
            if name and not _same(actor, name) and name not in found:
                found.append(name)
    return found


# --- may the verified repair be pushed ----------------------------------------------


def decide_landing(
    *,
    diff_stats: Any = None,
    verify_result: str = "",
    budget: ChurnBudget | None = None,
    changes: Iterable[Mapping[str, Any]] = (),
    breaking: bool = False,
    actor: str = "",
    codeowners: str = "",
    recent_authors: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """``push``, ``escalate``, ``stop`` or ``pending_verify``, with the reasons.

    A repair that passes its verify command under CI's toolchain is pushed.
    It escalates only when verify fails, when it is a major version change
    or the catalog marks it breaking, or when it touches a sensitive path.
    Churn is recorded for the note; on its own it never stops a repair.
    Ownership is not a gate: the owners are told afterwards.
    """
    stats = parse_numstat(diff_stats) if diff_stats is not None else {"files": 0, "lines": 0, "paths": []}
    over = bool(
        budget
        and stats["files"]
        and (stats["lines"] > budget.max_lines or stats["files"] > budget.max_files)
    )
    churn = {
        "files": stats["files"],
        "lines": stats["lines"],
        "budget": budget.to_dict() if budget else None,
        "over_budget": over,
    }
    reasons: list[str] = []
    majors = [c for c in changes if c.get("major_change")]
    if majors:
        reasons.append(
            "major version change: "
            + ", ".join(f"{c['package']} {c['current']} -> {c['expected']}" for c in majors)
        )
    if breaking:
        reasons.append("the catalog marks this repair as breaking")
    sensitive = [p for p in stats["paths"] if path_matches(p, SENSITIVE_GLOBS)]
    if sensitive:
        reasons.append(f"touches security-sensitive paths: {', '.join(sensitive)}")
    verify = (verify_result or "").strip().lower()
    if verify in {"fail", "failed", "failure"}:
        reasons.append(
            "verify failed after the repair"
            + (f" (churn {stats['lines']} lines over the budget)" if over else "")
        )
    notify = owners_to_tell(
        stats["paths"], actor=actor, codeowners=codeowners, recent_authors=recent_authors
    )
    if reasons:
        decision = "escalate"
    elif diff_stats is not None and stats["files"] == 0:
        decision = "stop"
        reasons.append("the repair changed nothing, so it did not repair the failure")
    elif verify in {"pass", "passed", "success"}:
        decision = "push"
    else:
        decision = "pending_verify"
    return {"decision": decision, "reasons": reasons, "churn": churn, "notify": notify}


# --- one repair per drift ----------------------------------------------------------


def coalesce(key: str, pr: str, in_flight: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """``repair`` when no other pull request is repairing ``key``; else ``wait``."""
    for item in in_flight or ():
        holder = str(item.get("pr") or "")
        if str(item.get("key") or "") == key and holder and holder != str(pr):
            return {"action": "wait", "waiting_on": holder, "key": key}
    return {"action": "repair", "waiting_on": "", "key": key}


# --- what people are told ----------------------------------------------------------


def blocked_pr_comment(*, check: str, base: str, detail: str, evidence: Iterable[str]) -> str:
    """The comment on a blocked pull request: it was not the pull request's doing."""
    because = "; ".join(evidence) or "the failure does not come from this change"
    return (
        f"`{check}` failed because of base drift, not caused by this pull request: {detail} "
        f"Evidence: {because}. Rivet is repairing it once; this pull request is re-run "
        f"after the repair is on `{base}`. Nothing is needed here."
    )


def fyi_note(
    *,
    check: str,
    base: str,
    entry_id: str,
    detail: str,
    commands: Iterable[str],
    verify: str,
    toolchain_pin: str,
    churn: Mapping[str, Any],
    owners: Iterable[str],
    carried_by: str,
) -> str:
    """A short note for the owners of the touched paths, after the push.

    It says what drifted, what Rivet changed, how it was verified, and how
    to revert. It reports; it does not ask.
    """
    who = ", ".join(owners)
    tried = "; ".join(commands) or "(no commands)"
    lines = [
        f"FYI {who}: `{check}` went red on pull requests that did not cause it ({entry_id}).",
        f"What drifted: {detail}",
        f"What Rivet changed: ran {tried} under {toolchain_pin or 'CI toolchain'} "
        f"(CI's toolchain), {churn.get('files', 0)} files, {churn.get('lines', 0)} lines.",
        f"How it was verified: {verify or 'the failing check'} passed before the push.",
        f"Where: commit `rivet: base-drift repair — {entry_id}` on {carried_by}; `{base}` "
        "carries it once that lands.",
        "To revert: `git revert` that commit. No action is needed otherwise.",
    ]
    return "\n".join(lines)


__all__ = [
    "NODE_BUNDLED_NPM",
    "SENSITIVE_GLOBS",
    "attribute_failure",
    "blocked_pr_comment",
    "choose_execution",
    "coalesce",
    "codeowners_for",
    "decide_landing",
    "detect_toolchain",
    "fyi_note",
    "owners_to_tell",
    "parse_numstat",
    "path_matches",
    "render_commands",
    "version_changes",
    "workflow_paths",
]
