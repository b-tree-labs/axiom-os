#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Give every worktree an ``.envrc`` that points its tools at its own source.

    python scripts/setup_worktree_envs.py                  # this repo's worktrees
    python scripts/setup_worktree_envs.py --repo ../nos --repo ../CoreForge
    python scripts/setup_worktree_envs.py --check          # report, change nothing
    python scripts/setup_worktree_envs.py --all            # every repo in the workspace

One virtualenv serves every worktree of every repo in this workspace, so
``pip install -e`` anchors each package to whichever checkout ran it. In every
other worktree the tools import the anchor's code: a verb you just added "does
not exist", a fix you just made "did not work", a test that passes on somebody
else's branch.

``.envrc`` is gitignored — correctly, people keep local secrets there — so the
fix cannot ship as a file. This is the one command that applies it everywhere,
including repos that are not this one: the hazard is a property of the shared
venv, not of any single project.

Layouts are detected rather than assumed. It was first written for one repo's
shape and then quietly did nothing for the two others in this workspace, which
is the same false green it exists to prevent.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

MARKER = "# >>> worktree source path"
END_MARKER = "# <<< worktree source path"

#: Directories that hold packages but are never themselves importable.
_NOT_A_PACKAGE_ROOT = {"tests", "test", "docs", "scripts", "examples", "build"}


def source_roots(worktree: Path) -> list[Path]:
    """The directories that must be on ``PYTHONPATH`` for THIS checkout.

    Three shapes appear in this workspace and all three are handled:

    * ``src/`` layout — the common one.
    * ``packages/<name>/src`` — a repo shipping more than one distribution.
    * a package directory at the repo root, with no ``src/`` at all.

    Returns an empty list for a directory that is not a Python project, which
    is how a docs or infrastructure repo is skipped rather than mangled.
    """
    roots: list[Path] = []

    src = worktree / "src"
    if src.is_dir() and any(child.is_dir() for child in src.iterdir()):
        roots.append(src)

    packages = worktree / "packages"
    if packages.is_dir():
        roots.extend(
            sorted(p / "src" for p in packages.iterdir() if (p / "src").is_dir())
        )

    if not roots:
        # No src/, so an importable package may sit at the root — CoreForge is
        # laid out this way. The repo root itself goes on the path, once.
        for child in sorted(worktree.iterdir()):
            if (
                child.is_dir()
                and child.name not in _NOT_A_PACKAGE_ROOT
                and not child.name.startswith(".")
                and (child / "__init__.py").is_file()
            ):
                roots.append(worktree)
                break

    return roots


def block_for(roots: list[Path]) -> str:
    """The lines to write into a worktree's ``.envrc``.

    Self-contained, with the paths baked in, rather than sourcing a helper from
    the repo. A helper ships on a branch, and sourcing it would leave every
    worktree on an older branch unfixed until that merged — which is most of
    them, and exactly the ones running the anchor's code today. It also lets
    this work in repos that carry no helper at all.
    """
    quoted = " ".join(f'"{root}"' for root in roots)
    return "\n".join(
        [
            MARKER,
            "# One venv serves every worktree, so `pip install -e` anchors each",
            "# package to one checkout. This points THIS worktree's tools at",
            "# THIS worktree's source. Regenerate: setup_worktree_envs.py",
            f"for _d in {quoted}; do",
            '  [ -d "$_d" ] || continue',
            '  case ":$PYTHONPATH:" in',
            '    *":$_d:"*) ;;',
            '    *) PYTHONPATH="$_d${PYTHONPATH:+:$PYTHONPATH}" ;;',
            "  esac",
            "done",
            "export PYTHONPATH",
            "unset _d",
            END_MARKER,
        ]
    )


def worktrees(repo: Path) -> list[Path]:
    """Every worktree of ``repo``, from git rather than a guess at the layout."""
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        Path(line.removeprefix("worktree ").strip())
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def envrc_is_tracked(worktree: Path) -> bool:
    """Whether ``.envrc`` is under version control here.

    A gitignored ``.envrc`` is local configuration and the right place for a
    per-worktree path. A TRACKED one is the project's file, shared with
    everyone, and writing a developer's absolute paths into it would commit
    them to the repository.

    This is not hypothetical caution. The first version assumed ``.envrc`` was
    gitignored everywhere because it is in one repo, and modified it in
    fourteen worktrees of another where it is tracked.
    """
    return (
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".envrc"],
            cwd=worktree,
            capture_output=True,
        ).returncode
        == 0
    )


def classify(worktree: Path) -> str:
    """``done`` | ``needs`` | ``tracked-envrc`` | ``not-a-project``.

    Three outcomes rather than a boolean, because the third is the interesting
    one and collapsing it into "nothing to do" is a lie. The first version of
    this script reported thirteen unfixed worktrees as already fine, which is
    exactly the false green it was written to prevent.
    """
    if not source_roots(worktree):
        return "not-a-project"
    if envrc_is_tracked(worktree):
        return "tracked-envrc"
    envrc = worktree / ".envrc"
    if not envrc.is_file():
        return "needs"
    text = envrc.read_text(encoding="utf-8")
    if MARKER not in text:
        return "needs"
    # A worktree fixed before this script learned a layout may carry a block
    # naming the wrong roots. Regenerate when what is written does not match
    # what this checkout actually needs.
    return "done" if all(str(r) in text for r in source_roots(worktree)) else "needs"


def apply(worktree: Path) -> None:
    """Add the block, keeping whatever was already there.

    Appends rather than rewrites, and is bounded by markers so a re-run
    replaces its own block and nothing else. Local secrets live in these files;
    that is why they are gitignored, and why this must never truncate one.
    """
    envrc = worktree / ".envrc"
    existing = envrc.read_text(encoding="utf-8") if envrc.is_file() else ""
    if not existing:
        existing = "source_up_if_exists\n"
    for old in (MARKER, "# >>> axiom worktree source path"):
        end = END_MARKER if old == MARKER else "# <<< axiom worktree source path"
        if old in existing and end in existing:
            head, _, rest = existing.partition(old)
            _, _, tail = rest.partition(end)
            existing = head.rstrip("\n") + tail
    if existing and not existing.endswith("\n"):
        existing += "\n"
    envrc.write_text(
        f"{existing}{block_for(source_roots(worktree))}\n", encoding="utf-8"
    )


def allow(worktree: Path) -> str:
    """``direnv allow``, or say why it did not happen."""
    try:
        subprocess.run(
            ["direnv", "allow", str(worktree)],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        return "direnv not installed — run `direnv allow` yourself once it is"
    except subprocess.CalledProcessError as exc:
        return f"direnv allow failed: {exc.stderr.strip() or exc}"
    return "allowed"


def sibling_repos(start: Path) -> list[Path]:
    """Every git repo beside this one in the workspace.

    Behind ``--all``, never the default. The shared venv is a workspace-level
    fact, but this workspace holds ninety repos and most are other people's
    clones — dropping an ``.envrc`` into someone else's checkout because it
    happens to sit in the same directory is not a fix, it is a mess. The
    default is this repo; anything else is named.
    """
    workspace = start.parent
    found = []
    for child in sorted(workspace.iterdir()):
        if not child.is_dir():
            continue
        if (child / ".git").exists():
            found.append(child)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report, change nothing")
    parser.add_argument(
        "--repo",
        type=Path,
        action="append",
        default=None,
        help="a repo to fix (repeatable). Default: the repo this script is in.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="every git repo in the workspace, including ones you do not own",
    )
    args = parser.parse_args(argv)

    here = Path(__file__).resolve().parent.parent
    if args.all:
        repos = sibling_repos(here)
    elif args.repo:
        repos = [p.resolve() for p in args.repo]
    else:
        repos = [here]

    trees: list[Path] = []
    for repo in repos:
        try:
            trees.extend(worktrees(repo))
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    trees = sorted(set(trees))

    grouped: dict[str, list[Path]] = {
        "done": [],
        "needs": [],
        "tracked-envrc": [],
        "not-a-project": [],
    }
    for tree in trees:
        grouped[classify(tree)].append(tree)

    pending = grouped["needs"]
    tracked = grouped["tracked-envrc"]
    print(
        f"{len(trees)} worktree(s) across {len(repos)} repo(s): "
        f"{len(grouped['done'])} already point at their own source, "
        f"{len(pending)} do not, "
        f"{len(tracked)} keep .envrc under version control, "
        f"{len(grouped['not-a-project'])} are not Python projects."
    )

    if tracked:
        print(
            f"\nSkipped {len(tracked)} worktree(s) whose .envrc is TRACKED. Writing a\n"
            "developer's absolute paths into a shared file would commit them. Add the\n"
            "block by hand, or gitignore .envrc in that repo first:\n"
        )
        for tree in tracked:
            print(f"  {tree}")
        print()

    if not pending:
        return 0

    if args.check:
        print("\nWould fix:")
        for tree in pending:
            roots = ", ".join(
                str(r.relative_to(tree)) if r != tree else "." for r in source_roots(tree)
            )
            print(f"  {tree}\n      roots: {roots}")
        return 1

    print()
    for tree in pending:
        apply(tree)
        print(f"  {tree}  ({allow(tree)})")
    print(f"\n{len(pending)} worktree(s) now use their own source.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
