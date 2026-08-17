# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi release`` — release control surface (consolidated from
``axi rivet`` per the 2026-05-30 noun-convention policy).

Per ADR-056: CLI verbs are thin wrappers over registered skill
functions. Business logic lives in ``release.skills``; today, those
skills delegate to legacy handlers in ``_legacy_rivet_cli`` and
``_legacy_release_cli``.

Verb grammar fixes (per spec-aeos-0.1 §4.3.1 + the 2026-05-30 audit):

    OLD `axi release <noun>`     →  NEW `axi release <verb> <resource>`
    patterns                   →  list patterns
    watched                    →  list watched
    paused                     →  list paused
    close-stale                →  close stale
    `axi release <part>`       →  `axi release cut <part>`

RIVET remains the agent-persona name (the LLM character that reasons
over CI / failure patterns / release readiness); the CLI noun is the
purpose-named ``release``.
"""

from __future__ import annotations

import argparse
from typing import Any

from . import _legacy_rivet_cli as _rivet_legacy
from . import skills as release_skills

# Back-compat re-exports for code/tests that imported these symbols
# from the pre-migration `cli` module (which was the simple
# release-bump tool, now `_legacy_release_cli`).
from . import _legacy_release_cli  # noqa: E402
from ._legacy_release_cli import (  # noqa: F401, E402
    ReleaseInfo,
    ReleaseManager,
    ensure_repo_or_offer_init,
    git_available,
)


_PROG = "axi release"


def build_parser() -> argparse.ArgumentParser:
    legacy = _rivet_legacy.build_parser()
    legacy_sub = next(
        a for a in legacy._subparsers._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    legacy_subs = legacy_sub.choices

    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="release — version bumps + CI/release reasoning surface",
    )
    sub = parser.add_subparsers(dest="verb")

    # ---- `cut <part>` (was simple `axi release <part>`) -----------------
    cut_p = sub.add_parser("cut", help="Bump version, tag, push.")
    cut_p.add_argument("part", nargs="?", choices=["major", "minor", "patch"])
    cut_p.add_argument("--dry-run", action="store_true")
    cut_p.add_argument("--status", action="store_true")
    cut_p.add_argument("--changelog", action="store_true")
    cut_p.add_argument("--tag-only", action="store_true")
    cut_p.add_argument("--no-push", action="store_true")
    cut_p.add_argument("--skip-tests", action="store_true")
    cut_p.add_argument("--yes", "-y", action="store_true")

    # ---- imperative-leaf verbs (1:1 from rivet) -------------------------
    # `heartbeat` is the load-bearing dispatcher target: launchd / systemd
    # fire `axi release heartbeat` every `heartbeat_interval` seconds. Per
    # the autopsy of the 2026-05-30 → 2026-06-01 silence, omitting it from
    # this list silently bricked RIVET's heartbeat.jsonl writer for 28 hours.
    for leaf in ("status", "mode", "check", "plan", "sync",
                 "watch", "unwatch", "pause", "resume", "heartbeat"):
        if leaf in legacy_subs:
            legacy_p = legacy_subs[leaf]
            sub.add_parser(leaf, parents=[legacy_p], add_help=False,
                           conflict_handler="resolve",
                           help=(legacy_p.description or "")[:60])

    # ---- consolidating verbs --------------------------------------------
    list_p = sub.add_parser("list", help="List resource (patterns|watched|paused).")
    list_sub = list_p.add_subparsers(dest="resource", required=True)
    for res in ("patterns", "watched", "paused"):
        if res in legacy_subs:
            legacy_p = legacy_subs[res]
            list_sub.add_parser(res, parents=[legacy_p], add_help=False,
                                conflict_handler="resolve")

    close_p = sub.add_parser("close", help="Close resource (stale).")
    close_sub = close_p.add_subparsers(dest="resource", required=True)
    if "close-stale" in legacy_subs:
        legacy_p = legacy_subs["close-stale"]
        close_sub.add_parser("stale", parents=[legacy_p], add_help=False,
                             conflict_handler="resolve")

    return parser


def _args_to_params(args: argparse.Namespace) -> dict[str, Any]:
    return {k: v for k, v in vars(args).items() if v is not None}


_LEGACY_PART_VERBS = {"major", "minor", "patch"}


def _is_legacy_cut_invocation(argv: list[str]) -> bool:
    """Pre-migration form: bare `axi release <part>` or `axi release --status`."""
    if not argv:
        return False
    if argv[0] in _LEGACY_PART_VERBS:
        return True
    return argv[0] in {"--status", "--changelog", "--dry-run", "--tag-only",
                       "--no-push", "--skip-tests"}


def main(argv: list[str] | None = None) -> int:
    # Back-compat for the pre-migration positional form. Tests in
    # test_release.py patch `cli.git_available` and expect cli.main()
    # to consult it — so we route the legacy form straight through
    # `_legacy_release_cli.main` (which calls `git_available()` from
    # *its* module). We override that lookup transparently below.
    if argv is None:
        import sys as _sys
        argv = list(_sys.argv[1:])

    # Universal repo guard — applies to legacy and modern dispatch alike.
    # Tests patch both `git_available` and `ensure_repo_or_offer_init` on
    # this module (test_release.py::TestCLI), so the guard must read
    # attributes off this module (not import-binding) to honor the patches.
    import axiom.extensions.builtins.release.cli as _self
    if not _self.git_available():
        print("git: not found on PATH")
        return 1
    # Path arg is required by the underlying helper; tests patch this to
    # a no-arg lambda so the *args/**kwargs sink absorbs whatever we pass.
    from pathlib import Path as _Path
    if not _self.ensure_repo_or_offer_init(_Path.cwd(), assume_yes=True):
        return 1

    if _is_legacy_cut_invocation(argv):
        # Make _legacy_release_cli's `git_available` resolve to whatever
        # tests patched on THIS module — keeps the legacy contract.
        import axiom.extensions.builtins.release.cli as _self
        _legacy_release_cli.git_available = _self.git_available
        return _legacy_release_cli.main(argv)

    args = build_parser().parse_args(argv)
    if not getattr(args, "verb", None):
        args.verb = "cut"

    from axiom.infra.skills import SkillContext
    from axiom.infra.paths import get_user_state_dir
    import logging

    registry = release_skills.bind_default()
    ctx = SkillContext(
        registry=registry,
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.release"),
        user_prompt=None,
    )
    params = _args_to_params(args)
    result = registry.invoke(f"release.{args.verb}", params, ctx)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
