# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/copy`` and ``/export``: a reply or a transcript leaves the session, scanned and stamped (ADR-168).

Both refuse to move a secret out and name the class that matched, never the text. ``/copy`` carries the
reply's origin stamp (``where.render_stamp``) so a pasted answer says what produced it. ``/export`` writes
Markdown or round-trippable JSON, never overwrites, and with ``--seal`` signs the written bytes with the
node signer the attest extension already uses.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..command_registry import CliSpec, CommandContext, command
from ..where import render_stamp, to_dict, where_report
from . import _transcript as tx

SCHEMA = "axiom.chat.export/1"
_FORMATS = ("md", "json")


# ---- /copy --------------------------------------------------------------------------------------


@command("/copy", "Copy the last reply (or /copy n) to the clipboard", "Session",
         usage="/copy [n] [--bare]", surfaces=frozenset({"repl", "tui"}))
def copy_cmd(ctx: CommandContext, args: list[str]) -> str:
    bare = "--bare" in args
    nums = [a for a in args if not a.startswith("--")]
    n = 1
    if nums:
        if not nums[0].isdigit() or int(nums[0]) < 1:
            return "\n  Usage: /copy [n] [--bare]   (n counts back from the latest reply)\n"
        n = int(nums[0])
    replies = tx.replies(ctx.agent)
    if not replies:
        return "\n  There is no assistant reply to copy yet.\n"
    if n > len(replies):
        return f"\n  Only {len(replies)} assistant repl{'y' if len(replies) == 1 else 'ies'} in this session; /copy {n} is out of range.\n"
    text = replies[-n].content

    hit = tx.scan(text)
    if hit:
        return (
            f"\n  Not copied: the reply matches a secret pattern ({tx.describe(hit)}). "
            "The text is not shown here.\n"
            f"  {tx.brand_cli()} will not put it on the clipboard; use /export --redact to write a "
            "copy with the matches removed.\n"
        )

    note = ""
    if not bare:
        report = where_report(ctx.agent)
        if n == 1 and report.basis == "answered":
            text = f"{text.rstrip()}\n\n{render_stamp(report)}"
        else:
            note = (
                "  No origin stamp added: only the latest reply's origin is known "
                "(per-reply stamps are not recorded yet).\n"
            )

    from ..clipboard import copy_to_system_clipboard

    if copy_to_system_clipboard(text):
        return f"\n  Copied reply {'' if n == 1 else f'-{n} '}({len(text)} characters) to the clipboard.\n{note}"
    return (
        "\n  No clipboard tool found (pbcopy, xclip or xsel), so nothing was placed on the OS clipboard. "
        "The text was sent as a terminal clipboard escape (OSC 52), which many terminals accept.\n"
        "  To get the text out reliably, use /export.\n"
    )


# ---- /export ------------------------------------------------------------------------------------


def _parse(ctx: CommandContext, args: list[str]) -> dict[str, Any]:
    try:
        toks = shlex.split(ctx.line)[1:] if ctx.line else list(args)
    except ValueError:
        toks = list(args)
    opts: dict[str, Any] = {"path": None, "format": None, "redact": False, "seal": False}
    it = iter(toks)
    for t in it:
        if t == "--redact":
            opts["redact"] = True
        elif t == "--seal":
            opts["seal"] = True
        elif t == "--format":
            opts["format"] = next(it, "")
        elif t.startswith("--format="):
            opts["format"] = t.split("=", 1)[1]
        elif t.startswith("--"):
            opts["error"] = f"unknown option {t}"
        elif opts["path"] is None:
            opts["path"] = t
        else:
            opts["error"] = "give one path (quote it if it contains spaces)"
    return opts


def _document(agent: Any) -> dict[str, Any]:
    session = agent.session
    report = where_report(agent)
    return {
        "schema": SCHEMA,
        "exported_at": datetime.now(UTC).isoformat(),
        "session": {
            "session_id": session.session_id,
            "title": session.title,
            "created_at": session.created_at,
            "principal_id": session.principal_id,
        },
        "where": to_dict(report),
        "where_stamp": render_stamp(report),
        "stamps_recorded": False,
        "stamps_note": "per-reply stamps are not recorded yet; `where` is the session's current origin",
        "redactions": {},
        "messages": [m.to_dict() for m in session.messages],
    }


def _markdown(doc: dict[str, Any]) -> str:
    s = doc["session"]
    n = sum(doc["redactions"].values())
    lines = [
        f"# {s['title'] or 'Chat transcript'}",
        "",
        f"- Session: {s['session_id']}",
        f"- Exported: {doc['exported_at']}",
        f"- Origin (current): {doc['where_stamp']}",
        f"- Note: {doc['stamps_note']}.",
    ]
    if n:
        lines.append(
            f"- {n} redaction{'s' if n != 1 else ''} applied ({tx.describe_dict(doc['redactions'])})."
        )
    lines.append("")
    for m in doc["messages"]:
        who = {"user": "You", "assistant": "Assistant"}.get(m["role"], m["role"].title())
        lines += [f"## {who}", "", m["content"].rstrip(), ""]
        names = [str(c.get("name", "?")) for c in m.get("tool_calls", []) if isinstance(c, dict)]
        if names:
            lines += [f"_tool calls: {', '.join(names)}_", ""]
    return "\n".join(lines)


def _target(opts: dict[str, Any], session_id: str, fmt: str) -> Path:
    if opts["path"]:
        return Path(opts["path"]).expanduser()
    from axiom.infra.paths import get_user_state_dir

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return get_user_state_dir() / "exports" / f"{session_id}-{stamp}.{fmt}"


def _write_new(path: Path, data: bytes) -> Path:
    """Create ``path`` exclusively. A name taken by an earlier default export gets a numeric suffix; an
    explicit path that exists is the caller's to resolve (FileExistsError)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as fh:
        fh.write(data)
    return path


def _seal(path: Path, data: bytes, fmt: str, doc: dict[str, Any]) -> str:
    try:
        from axiom.attest.chain import GENESIS, seal
        from axiom.extensions.builtins.attest import signing

        signer = signing.signer()
        record = seal(
            {
                "kind": "chat-export",
                "file": path.name,
                "file_sha256": hashlib.sha256(data).hexdigest(),
                "format": fmt,
                "session_id": doc["session"]["session_id"],
                "exported_at": doc["exported_at"],
                "redactions": sum(doc["redactions"].values()),
            },
            seq=1,
            prev_digest=GENESIS,
            signer=signer,
        )
        sidecar = path.with_name(path.name + ".seal.json")
        _write_new(sidecar, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    except Exception as exc:  # noqa: BLE001 - say why, do not hide the export
        return f"  Unsigned: {exc}\n"
    return f"  Sealed with {record['node_sig']['key_id']}: {sidecar}\n"


@command("/export", "Write the transcript to a file (md or json; --redact, --seal)", "Session",
         usage="/export [path] [--format md|json] [--redact] [--seal]", mutates=True,
         surfaces=frozenset({"repl", "tui"}),
         cli=CliSpec(("--export",), line="/export", applies="headless", metavar="SESSION", session_arg=True,
                     modifiers=(("--format", ("md", "json")),),
                     help="write a stored session's transcript to a file and exit (--format md|json)"))
def export_cmd(ctx: CommandContext, args: list[str]) -> str:
    opts = _parse(ctx, args)
    if "error" in opts:
        return f"\n  {opts['error']}. Usage: /export [path] [--format md|json] [--redact] [--seal]\n"
    fmt = opts["format"] or (
        "json" if opts["path"] and str(opts["path"]).lower().endswith(".json") else "md"
    )
    if fmt not in _FORMATS:
        return f"\n  Unknown format {fmt!r}; choose md or json.\n"
    if not any(m.content.strip() for m in ctx.agent.session.messages):
        return "\n  Nothing to export: this session has no messages yet.\n"

    doc = _document(ctx.agent)
    found = tx.scan(doc)
    if found and not opts["redact"]:
        return (
            f"\n  Not exported: the transcript matches secret patterns ({tx.describe(found)}). "
            "Nothing was written and the text is not shown.\n"
            "  Re-run with --redact to write a copy with each match replaced by [redacted:<class>].\n"
        )
    if found:
        doc, counts = tx.redact(doc)
        doc["redactions"] = dict(counts)

    body = _markdown(doc) + "\n" if fmt == "md" else json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    data = body.encode("utf-8")
    path = _target(opts, ctx.agent.session.session_id, fmt)
    try:
        if opts["path"]:
            _write_new(path, data)
        else:
            base, n = path, 1
            while True:
                try:
                    path = _write_new(base if n == 1 else base.with_name(f"{base.stem}-{n}{base.suffix}"), data)
                    break
                except FileExistsError:
                    n += 1
    except FileExistsError:
        return f"\n  Not written: {path} already exists. Pick another path; /export never overwrites.\n"
    except OSError as exc:
        return f"\n  Could not write {path}: {exc.strerror or exc}\n"

    out = f"\n  Exported {len(ctx.agent.session.messages)} messages to {path}\n"
    n_red = sum(doc["redactions"].values())
    if n_red:
        out += f"  {n_red} redaction{'s' if n_red != 1 else ''} applied ({tx.describe_dict(doc['redactions'])}).\n"
    out += "  Per-reply origin stamps are not recorded yet; the file carries the session's current origin.\n"
    if opts["seal"]:
        out += _seal(path, data, fmt, doc)
    return out
