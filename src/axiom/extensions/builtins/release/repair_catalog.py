# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The repairs Rivet knows, as data.

Each entry names a failure by its log signature and says what to do about
it: who probably caused it, the commands that repair it, the toolchain the
commands must run under, how much change the repair may make, how to verify
it, how to resubmit, and when to stop. ``plan_repair`` reads this catalog in
order and takes the first active entry that matches.

A person who resolves a failure the catalog did not know can turn the fix
into a candidate entry with :func:`draft_entry`. A candidate is
``needs_review`` and is never used until a person makes it active.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

ATTRIBUTIONS = ("caused_by_pr", "base_drift", "undetermined")


@dataclass(frozen=True)
class ChurnBudget:
    """The most change a mechanical repair may make before a person looks."""

    max_lines: int
    max_files: int

    def to_dict(self) -> dict[str, int]:
        return {"max_lines": self.max_lines, "max_files": self.max_files}


@dataclass(frozen=True)
class CatalogEntry:
    """One known failure and its repair.

    ``signatures`` are case-insensitive regular expressions searched over
    the check name and the log; any one matching is a match. ``inputs`` are
    the globs of the files this failure is about (a lockfile, a manifest);
    a pull request that changes one of them probably caused the failure.
    ``inputs_complete`` says the failure depends only on those files and
    the outside world, so a pull request that changes none of them did not
    cause it.
    ``toolchain`` names the runtime the commands need (``node``,
    ``python``) so they are pinned to CI's version, never the local one.
    The harness runs the commands inside CI's runtime image (see
    ``ci_drift.render_commands``), so ``npm`` there is CI's npm. ``escalate`` marks an entry with no mechanical repair (a
    person decides); ``breaking`` marks a repair that is never pushed
    without a person. ``churn_budget`` is recorded for the owners' note; it
    does not stop a repair whose verify passes.
    """

    id: str
    signatures: tuple[str, ...]
    detail: str
    default_attribution: str = "caused_by_pr"
    commands: tuple[str, ...] = ()
    toolchain: str = ""
    churn_budget: ChurnBudget | None = None
    verify: str = ""
    resubmit: str = "push"
    stop_conditions: tuple[str, ...] = ()
    escalate: bool = False
    breaking: bool = False
    inputs: tuple[str, ...] = ()
    inputs_complete: bool = False
    status: str = "active"
    origin: dict[str, Any] = field(default_factory=dict)

    def matches(self, text: str) -> bool:
        return any(re.search(sig, text, re.IGNORECASE) for sig in self.signatures)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "signatures": list(self.signatures),
            "detail": self.detail,
            "default_attribution": self.default_attribution,
            "commands": list(self.commands),
            "toolchain": self.toolchain,
            "churn_budget": self.churn_budget.to_dict() if self.churn_budget else None,
            "verify": self.verify,
            "resubmit": self.resubmit,
            "stop_conditions": list(self.stop_conditions),
            "escalate": self.escalate,
            "breaking": self.breaking,
            "inputs": list(self.inputs),
            "inputs_complete": self.inputs_complete,
            "status": self.status,
            "origin": dict(self.origin),
        }


_NODE_INPUTS = (
    "**/package.json",
    "**/package-lock.json",
    "**/npm-shrinkwrap.json",
    "**/yarn.lock",
    "**/pnpm-lock.yaml",
)

#: Order matters: the first active match wins. The first five are the
#: repairs Rivet had before the catalog, in their original order, so a log
#: that matched one of them still gets exactly the same plan.
CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        id="context-sync",
        signatures=(r"context files drifted", r"agents\.md"),
        detail="The generated context files drifted from AGENTS.md.",
        commands=("axi context sync",),
        resubmit="push",
    ),
    CatalogEntry(
        id="ruff-fix",
        signatures=(r"ruff",),
        detail="Ruff reported a finding it can fix.",
        commands=("ruff check src tests conftest.py --select E,F,W --ignore E501 --fix",),
        resubmit="push",
    ),
    CatalogEntry(
        id="short-run",
        signatures=(r"barely ran", r"too short to prove"),
        detail="The run was too short to prove the property, and nothing torn was reported.",
        default_attribution="undetermined",
        resubmit="rerun",
    ),
    CatalogEntry(
        id="stray-root",
        signatures=(r"unexpected root",),
        detail="A directory at the repo root is not part of the tree. It is not deleted from here.",
        resubmit="none",
        stop_conditions=("a person removes or keeps the directory",),
    ),
    CatalogEntry(
        id="customer-name",
        signatures=(r"forbidden term", r"name a deployment", r"names a customer"),
        detail=(
            "The platform tree names a customer or a deployment. That is rewritten "
            "by a person, not by a fixer."
        ),
        resubmit="none",
        stop_conditions=("a person rewrites the text",),
    ),
    # --- Drift: the base or an upstream release moved, not the pull request.
    CatalogEntry(
        id="expo-expected-version",
        signatures=(
            r"expected version: ~?\^?\d",
            r"found outdated dependencies",
            r"expo sdk .*mismatch",
            r"install the expected versions of the packages",
        ),
        detail=(
            "The app's dependencies no longer match the framework's expected-version "
            "table. An upstream release usually moves that table, so every pull "
            "request fails until the base is bumped."
        ),
        default_attribution="base_drift",
        commands=("npx expo install --fix",),
        toolchain="node",
        churn_budget=ChurnBudget(max_lines=400, max_files=4),
        verify="npx expo install --check",
        resubmit="push",
        stop_conditions=(
            "the verify command still reports outdated dependencies",
            "an expected version is a major version change",
        ),
        inputs=_NODE_INPUTS + ("**/app.json", "**/app.config.*"),
        inputs_complete=True,
    ),
    CatalogEntry(
        id="npm-lockfile-out-of-sync",
        signatures=(
            r"lock file'?s .* does not satisfy",
            r"can only install packages when your package\.json and package-lock\.json",
            r"package-lock\.json.* (are|is) (not )?in sync",
        ),
        detail="The lockfile does not satisfy package.json, so a clean install refuses it.",
        default_attribution="caused_by_pr",
        commands=("npm install --package-lock-only --ignore-scripts",),
        toolchain="node",
        churn_budget=ChurnBudget(max_lines=300, max_files=2),
        verify="npm ci --ignore-scripts",
        resubmit="push",
        stop_conditions=("the verify install still fails",),
        inputs=_NODE_INPUTS,
        inputs_complete=True,
    ),
    CatalogEntry(
        id="pip-resolver-conflict",
        signatures=(
            r"resolutionimpossible",
            r"conflicting dependencies",
            r"pip's dependency resolver",
        ),
        detail=(
            "The Python resolver found no set of versions that satisfies every "
            "requirement. A new upstream release is the usual cause; choosing a pin "
            "is a person's call."
        ),
        default_attribution="base_drift",
        toolchain="python",
        verify="python -m pip install --dry-run -r requirements.txt",
        resubmit="none",
        escalate=True,
        stop_conditions=("a person chooses which requirement to pin or relax",),
        inputs=(
            "**/requirements*.txt",
            "**/pyproject.toml",
            "**/setup.cfg",
            "**/setup.py",
            "**/constraints*.txt",
            "**/poetry.lock",
            "**/uv.lock",
        ),
        inputs_complete=True,
    ),
    CatalogEntry(
        id="action-runtime-deprecated",
        signatures=(
            r"node\.?js \d+ actions are deprecated",
            r"the `?(set-output|save-state)`? command is deprecated",
            r"this request has been automatically failed because it uses a deprecated version of",
        ),
        detail=(
            "A workflow uses an action or runtime the host has deprecated, and the "
            "deprecation now fails the job. The fix is a newer major of each named "
            "action in the workflow files."
        ),
        default_attribution="base_drift",
        resubmit="none",
        escalate=True,
        stop_conditions=("workflow files are a protected path; a person approves the bump",),
        inputs=(".github/workflows/**",),
    ),
    CatalogEntry(
        id="toolchain-version-not-found",
        signatures=(
            r"unable to find node version",
            r"version '?[\w.\-]+'? with architecture '?\w+'? was not found",
            r"could not find a version that satisfies the requirement python",
        ),
        detail=(
            "The workflow asks setup-node or setup-python for a version the runner "
            "image cannot provide. The pin in the workflow needs a version that exists."
        ),
        default_attribution="base_drift",
        resubmit="none",
        escalate=True,
        stop_conditions=("workflow files are a protected path; a person chooses the version",),
        inputs=(".github/workflows/**", "**/.nvmrc", "**/.node-version", "**/.python-version"),
    ),
)


def match_entry(text: str, catalog: Iterable[CatalogEntry] | None = None) -> CatalogEntry | None:
    """The first active entry whose signature matches ``text``."""
    for entry in CATALOG if catalog is None else catalog:
        if entry.status == "active" and entry.matches(text):
            return entry
    return None


_GENERIC_LINE = re.compile(
    r"process completed with exit code|exited with code|^\s*$|^error:?\s*$", re.IGNORECASE
)
_ERROR_LINE = re.compile(r"##\[error\]|\berror\b|\bfailed\b|\bERR!", re.IGNORECASE)
_VOLATILE = re.compile(r"\d+(?:\.\d+)*")


def _signature_from(line: str) -> str:
    text = re.sub(r"^\s*(##\[error\]|npm ERR!|ERROR:)\s*", "", line.strip(), flags=re.IGNORECASE)
    parts = _VOLATILE.split(text)
    return r"\d+(?:\.\d+)*".join(re.escape(part).replace("\\ ", " ") for part in parts)


def draft_entry(
    log: str,
    commands: Iterable[str],
    *,
    verify: str = "",
    resolved_by: str = "",
    entry_id: str = "",
) -> CatalogEntry:
    """A candidate entry from a failure a person resolved by hand.

    The signature is built from the log's distinctive error lines, with
    numbers generalised and generic exit-code lines dropped. The entry is
    ``needs_review``: :func:`match_entry` skips it until a person reviews
    it and makes it active.
    """
    lines = [line for line in log.splitlines() if not _GENERIC_LINE.search(line)]
    chosen = [line for line in lines if _ERROR_LINE.search(line)] or lines[:1]
    signatures = tuple(dict.fromkeys(_signature_from(line) for line in chosen[:3] if line.strip()))
    first = _signature_from(chosen[0]) if chosen else "candidate"
    slug = entry_id or re.sub(r"[^a-z0-9]+", "-", re.sub(r"\\d.*?\*", "", first).lower())
    slug = slug.strip("-")[:48] or "candidate"
    return CatalogEntry(
        id=slug,
        signatures=signatures,
        detail="Drafted from a failure a person resolved. Review the signature and the commands.",
        default_attribution="undetermined",
        commands=tuple(commands),
        verify=verify,
        resubmit="push",
        stop_conditions=("not reviewed",),
        status="needs_review",
        origin={"resolved_by": resolved_by, "log_excerpt": "\n".join(chosen[:3])},
    )


__all__ = [
    "ATTRIBUTIONS",
    "CATALOG",
    "CatalogEntry",
    "ChurnBudget",
    "draft_entry",
    "match_entry",
]
