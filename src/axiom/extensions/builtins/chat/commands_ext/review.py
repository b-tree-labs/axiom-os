# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""/review [path | --staged | --branch <b>]: review a diff, read-only (ADR-168).

The command gathers the diff itself with read-only git calls, bounds it with a stated limit, and routes
the bundled ``templates/review.md`` prompt through the agent for ONE turn in plan semantics (tools withheld,
mutating calls refused; see ``/plan``). The standing mode is not changed.

A ``review.md`` in ``~/.axi/commands`` or ``<project>/.<cli>/commands`` replaces the bundled template. Its
body may use ``$ARGUMENTS`` and ``$DIFF``. An override is yours: it runs in whatever mode you are in, because
a template file cannot declare a mode.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..command_registry import CommandContext, command
from ..follow_ups import FollowUp, pick
from ..user_commands import UserCommandPrompt, render_command
from ._bundled import bundled_path, resolve

#: Characters of diff text sent to the model. Beyond this the diff is cut and the prompt says so.
MAX_DIFF_CHARS = 60_000

#: Files never sent to the model: credentials by name. Excluded with a pathspec and named in the prompt.
SECRET_PATHSPECS = (
    ":(exclude,glob)**/.env",
    ":(exclude,glob)**/.env.*",
    ":(exclude,glob)**/*.pem",
    ":(exclude,glob)**/*.key",
    ":(exclude,glob)**/id_rsa*",
    ":(exclude,glob)**/id_ed25519*",
    ":(exclude,glob)**/*credentials*",
)

_GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"}


class ReviewError(Exception):
    """A request that cannot be turned into a diff; the message is shown to the user."""


@dataclass(frozen=True)
class Gathered:
    text: str  # diff text, possibly truncated
    scope: str  # human description of what was diffed
    truncated_from: int  # original length when truncated, else 0
    untracked: tuple[str, ...]
    excluded: bool


def _git(root: Path, *args: str) -> str:
    # Read-only verbs only (diff / status / rev-parse); no external diff or textconv programs are run.
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, env=_GIT_ENV, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise ReviewError((proc.stderr or proc.stdout).strip() or f"git {args[0]} failed")
    return proc.stdout


def parse_args(args: list[str]) -> tuple[str, str]:
    """``(kind, value)`` where kind is ``tree`` | ``staged`` | ``branch`` | ``path``."""
    if not args:
        return "tree", ""
    if args[0] == "--staged":
        return "staged", ""
    if args[0] == "--branch":
        if len(args) < 2:
            raise ReviewError("usage: /review --branch <name>")
        return "branch", args[1]
    if args[0].startswith("-"):
        raise ReviewError(f"unknown option {args[0]}; use a path, --staged or --branch <name>")
    return "path", " ".join(args)


def gather_diff(root: Path, args: list[str], limit: int = MAX_DIFF_CHARS) -> Gathered:
    kind, value = parse_args(args)
    try:
        _git(root, "rev-parse", "--show-toplevel")
    except ReviewError:
        raise ReviewError(f"{root} is not inside a git repository") from None
    base = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    paths: list[str] = []
    if kind == "staged":
        base.append("--cached")
        scope = "staged changes"
    elif kind == "branch":
        if value.startswith("-"):
            raise ReviewError(f"{value!r} is not a branch name")
        try:
            _git(root, "rev-parse", "--verify", "--quiet", f"{value}^{{commit}}")
        except ReviewError:
            raise ReviewError(f"no such branch or commit: {value}") from None
        base.append(f"{value}...HEAD")
        scope = f"{value}...HEAD (changes on this branch since it diverged)"
    else:
        base.append("HEAD")
        scope = "working tree against HEAD (staged and unstaged, tracked files)"
        if kind == "path":
            target = (root / value).resolve()
            if not target.exists():
                raise ReviewError(f"no such path: {value}")
            paths = [str(target.relative_to(root.resolve()))] if target.is_relative_to(root.resolve()) else []
            if not paths:
                raise ReviewError(f"{value} is outside the project")
            scope += f", limited to {paths[0]}"
    pathspec = ["--", *(paths or ["."]), *SECRET_PATHSPECS]
    stat = _git(root, *base, "--stat", *pathspec)
    body = _git(root, *base, *pathspec)
    everything = _git(root, *base, "--name-only", "--", *(paths or ["."]))
    kept = _git(root, *base, "--name-only", *pathspec)
    excluded = set(everything.split()) - set(kept.split())
    untracked: tuple[str, ...] = ()
    if kind in ("tree", "path"):
        porcelain = _git(root, "status", "--porcelain", "--untracked-files=all", "--", *(paths or ["."]))
        untracked = tuple(ln[3:] for ln in porcelain.splitlines() if ln.startswith("?? "))
    text = (stat + "\n" + body) if body else ""
    cut = 0
    if len(text) > limit:
        cut = len(text)
        text = text[:limit]
    return Gathered(text=text, scope=scope, truncated_from=cut, untracked=untracked,
                    excluded=bool(excluded))


def render_diff_block(g: Gathered, limit: int = MAX_DIFF_CHARS) -> str:
    lines = [f"Diff scope: {g.scope}", ""]
    if g.untracked:
        lines += ["Untracked files (not in the diff below, review them if relevant): " + ", ".join(g.untracked[:50]), ""]
    if g.excluded:
        lines += ["Credential-like files (.env, keys, certificates) were excluded from this diff by name.", ""]
    lines += ["```diff", g.text.rstrip("\n"), "```"]
    if g.truncated_from:
        lines += ["", f"NOTE: the diff was truncated to the first {limit:,} of {g.truncated_from:,} characters. "
                  "Later files are NOT shown; say so rather than guessing, and ask for a narrower review "
                  "(/review <path>)."]
    return "\n".join(lines)


def _project_root() -> Path:
    from axiom.infra.paths import get_project_root

    return get_project_root()


FOLLOW_UPS = {
    "agent": FollowUp(
        "/agent", "full tool loop, so the agent can apply a fix it found; every write still asks for approval"
    ),
    "diff": FollowUp("/diff", "see exactly what an applied fix changed"),
    "undo": FollowUp("/undo", "put back what the agent wrote if you do not want it"),
    "staged": FollowUp("/review --staged", "review only what is staged"),
}


@command(
    "/review", "Review the working tree diff, read-only (/review [path|--staged|--branch <b>])", "Agent",
    usage="/review [path|--staged|--branch <b>]",
    follow_ups=FOLLOW_UPS,
    subcommands={"--staged": "Review staged changes", "--branch": "Review changes since a branch diverged",
                 "--template": "Show the bundled template and how to customise it"},
)
def _review(ctx: CommandContext, args: list[str]):
    if args[:1] == ["--template"]:
        from axiom.infra.branding import get_branding

        cli = get_branding().cli_name
        return (f"\n  Bundled template: {bundled_path('review')}\n"
                f"  Copy it to ~/.{cli}/commands/review.md (or <project>/.{cli}/commands/review.md) and edit;\n"
                "  a file there replaces the bundled one. Placeholders: $ARGUMENTS and $DIFF.\n")
    try:
        gathered = gather_diff(_project_root(), args)
    except ReviewError as exc:
        return f"\n  /review: {exc}\n"
    if not gathered.text.strip():
        extra = f" (untracked: {', '.join(gathered.untracked[:10])})" if gathered.untracked else ""
        from ..follow_ups import result

        return result(
            "/review", f"\n  Nothing to review: no diff for {gathered.scope}.{extra}\n",
            "staged" if "--staged" not in args else "diff",
        )
    cmd, overridden = resolve("review")
    scope_text = " ".join(args) or "the working tree"
    prompt = render_command(cmd, scope_text).replace("$DIFF", render_diff_block(gathered))
    if "$DIFF" not in cmd.body:  # a customised template that forgot $DIFF still gets the diff
        prompt += "\n\n" + render_diff_block(gathered)
    if not overridden:
        ctx.agent.request_next_turn_mode("plan")
    return UserCommandPrompt(
        command_name="review", prompt=prompt, follow_ups=pick("/review", "agent", "diff", "undo")
    )
