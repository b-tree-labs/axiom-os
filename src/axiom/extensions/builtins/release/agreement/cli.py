# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The agreement commands: ``agree``, ``prompts``, ``verify``, ``replay`` and ``hook``.

Reached as ``axi release agree`` and ``axi release agreement <verb>``, and as
``python -m axiom.extensions.builtins.release.agreement <verb>`` (the hook's fast path).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from . import hook, shadow, store
from .affirm import ACTION_WORDS
from .verify import Subject, verify


def _brand_cli() -> str:
    """The command the operator actually typed (a product's CLI name brands the command)."""
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{_brand_cli()} release agreement")
    sub = parser.add_subparsers(dest="verb", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--state-dir", dest="state_dir", default=None, help="state folder (default: ~/.axi)"
    )
    common.add_argument("--format", dest="format", choices=("text", "json"), default="text")

    sub.add_parser("hook", help="UserPromptSubmit hook: log the prompt on stdin. Silent, exit 0.")

    agree = sub.add_parser(
        "agree",
        parents=[common],
        help="Record that the owner agreed, citing a logged prompt by id.",
    )
    agree.add_argument("--repo", required=True, help="owner/name")
    agree.add_argument("--pr", type=int, required=True)
    agree.add_argument("--head", required=True, help="the head sha being agreed to")
    agree.add_argument("--message", required=True, help="id of the logged prompt (see: prompts)")
    agree.add_argument(
        "--work", required=True, help="the work, in words the assistant said to the owner"
    )
    agree.add_argument(
        "--paths", required=True, help="comma-separated paths or prefixes the work covers"
    )
    agree.add_argument(
        "--action",
        dest="actions",
        action="append",
        default=[],
        help="an irreversible step the owner agreed to by name ("
        + ", ".join(ACTION_WORDS)
        + "); repeatable",
    )
    agree.add_argument(
        "--handoff-kind",
        dest="handoff_kind",
        choices=("cross-session-message", "memory"),
        default=None,
    )
    agree.add_argument(
        "--handoff-ref",
        dest="handoff_ref",
        default=None,
        help="path of the message transcript or memory file",
    )

    prompts = sub.add_parser(
        "prompts", parents=[common], help="List recent logged prompts with their ids."
    )
    prompts.add_argument("--last", type=int, default=10)
    prompts.add_argument("--session", default=None)

    check = sub.add_parser(
        "verify", parents=[common], help="What the verifier says about one pull request."
    )
    check.add_argument("--repo", required=True)
    check.add_argument("--pr", type=int, required=True)
    check.add_argument("--head", default="", help="head sha (default: read from GitHub)")
    check.add_argument("--head-ref", dest="head_ref", default="")
    check.add_argument("--title", default="")
    check.add_argument(
        "--files", default="", help="comma-separated changed paths (default: read from GitHub)"
    )
    check.add_argument(
        "--first-commit", dest="first_commit", default="", help="ISO time of the first commit"
    )

    replay = sub.add_parser(
        "replay", parents=[common], help="Compare shadow rows with what the owner did on GitHub."
    )
    replay.add_argument(
        "--owner",
        dest="owners",
        action="append",
        default=[],
        help="owner login; repeatable (default: the repo's account)",
    )
    replay.add_argument("--repo", default=None)
    return parser


def _out(args: argparse.Namespace, value: object, text: str) -> None:
    print(json.dumps(value, indent=2, sort_keys=True) if args.format == "json" else text)


def _agree(args: argparse.Namespace) -> int:
    handoff = None
    if args.handoff_kind:
        handoff = {"kind": args.handoff_kind, "ref": args.handoff_ref or ""}
    try:
        record = store.create_record(
            args.state_dir,
            message_id=args.message,
            repo=args.repo,
            pr=args.pr,
            head_sha=args.head,
            work=args.work,
            paths=[p for p in args.paths.split(",")],
            actions=args.actions,
            handoff=handoff,
        )
    except store.AgreementRefused as exc:
        print(
            f"{exc}; nothing recorded. Only a prompt the hook logged can be cited.", file=sys.stderr
        )
        return 2
    value = {**record.__dict__, "paths": list(record.paths), "actions": list(record.actions)}
    _out(
        args,
        value,
        f"recorded {record.id} for {record.repo}#{record.pr} citing {record.message_id}",
    )
    return 0


def _prompts(args: argparse.Namespace) -> int:
    entries = [
        e
        for e in store.read_prompts(args.state_dir).entries
        if not args.session or e.session_id == args.session
    ]
    rows = [
        {
            "id": e.id,
            "time": e.time.isoformat(),
            "session_id": e.session_id,
            "shape": e.shape,
            "prompt": e.prompt[:120],
        }
        for e in entries[-args.last :]
    ]
    _out(
        args,
        rows,
        "\n".join(
            f"{r['id']}  {r['time']}  {r['session_id'][:8]}  {r['shape']:7}  {r['prompt']!r}"
            for r in rows
        ),
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    head, head_ref, title = args.head, args.head_ref, args.title
    files = tuple(f for f in args.files.split(",") if f)
    first = (
        datetime.fromisoformat(args.first_commit.replace("Z", "+00:00"))
        if args.first_commit
        else None
    )
    if not (head and files):
        import subprocess

        try:
            done = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(args.pr),
                    "-R",
                    args.repo,
                    "--json",
                    "headRefOid,headRefName,title,files,commits",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"cannot read the pull request from GitHub: {exc}", file=sys.stderr)
            return 3
        data = json.loads(done.stdout)
        head = head or data.get("headRefOid", "")
        head_ref = head_ref or data.get("headRefName", "")
        title = title or data.get("title", "")
        files = files or tuple(f.get("path", "") for f in data.get("files", ()))
        commits = data.get("commits") or []
        if first is None and commits and commits[0].get("committedDate"):
            first = datetime.fromisoformat(commits[0]["committedDate"].replace("Z", "+00:00"))
    subject = Subject(args.repo, args.pr, head, head_ref, title, (), files, first)
    verdict = verify(subject, state_dir=args.state_dir)
    value = {
        "decision": verdict.decision,
        "would_have_skipped_the_owners_github_review": verdict.skips_review,
        "reasons": list(verdict.reasons),
        "record_id": verdict.record_id,
        "uncovered": list(verdict.uncovered),
        "detail": verdict.detail,
    }
    _out(
        args,
        value,
        f"{verdict.decision} (skips the owner's review: {verdict.skips_review}): {verdict.detail}",
    )
    return 0


def _replay(args: argparse.Namespace) -> int:
    rows = [
        r for r in shadow.read_shadow(args.state_dir) if not args.repo or r.get("repo") == args.repo
    ]
    if not rows:
        _out(
            args,
            {"rows": 0},
            "no shadow rows yet: the lander records one for each pull request it evaluates "
            "on a machine where the prompt hook has run.",
        )
        return 0
    owners = tuple(args.owners) or tuple(sorted({str(r["repo"]).split("/")[0] for r in rows}))
    report = shadow.compare(rows, shadow.fetch_github, owners=owners)
    lines = [report["sentence"]]
    for key in ("yes", "uncertain", "no"):
        if key in report["would_skip"]:
            lines.append(
                f"  would skip review = {key}: "
                + ", ".join(f"{k} {v}" for k, v in sorted(report["would_skip"][key].items()))
            )
    for item in report["false_skips"]:
        lines.append(f"  false skip: {item['repo']}#{item['pr']} ({item['outcome']})")
    lines.append(
        f"  ready to go live: {'yes' if report['ready_to_go_live'] else 'no'} "
        f"(needs {report['minimum_sample']} known outcomes and at most {int(shadow.MAX_FALSE_SKIP_SHARE * 100)}% changed or rejected)"
    )
    _out(args, report, "\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "hook":
        return hook.main()
    args = build_parser().parse_args(argv)
    return {"agree": _agree, "prompts": _prompts, "verify": _verify, "replay": _replay}[args.verb](
        args
    )
