# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``<cli> log``: the command noun for logs (ADR-243, prd-attestation §6b).

    <cli> log <verb>                    things about all logs on this node
    <cli> log <log> <verb> [args]       one log, named by its handle
    <cli> log platform <verb>           Platform events (the machine's own log)

The first word is a reserved word (a noun-level verb, or ``platform``) or a log's handle; a
handle runs the one per-log parser. Every verb calls one existing skill through the registry
(ADR-056): this module parses arguments and prints, nothing else. ``SKILL_FOR_VERB`` maps each
verb path to its skill for ``cli/command_space.py``, which cannot join words when the second
one is a value.

Exit codes: 0 done; 1 refused, or a check failed; 2 usage (unknown log, verb or flag), with the
nearest match; 3 nothing was signed because the person held, asked or did not answer.

Code keeps ``logbook`` (skills, MCP tools, routes, the record); everything a person reads here
says "log". The old system log verbs (``log tail|verify|...``) forward to ``log platform`` for
two minor releases and then stay reserved, so an old script never reaches a log.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import sys
from dataclasses import replace
from typing import Any

from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillResult

from .logbooks import NOUN_LEVEL_WORDS, OLD_PLATFORM_WORDS, RESERVED_HANDLES

#: Exit code: nothing was signed (held, asked, or nobody answered at the prompt).
NOTHING_SIGNED = 3
#: The release that removes the old forms (``axi attest ...``, ``axi log tail``...).
FORWARDERS_REMOVED_IN = "v0.72.0"
QUIET_ENV = "AXI_NO_DEPRECATION_WARNING"
#: Why the last ``new`` or ``sign`` signed nothing: "answered" (hold or ask) or "nobody". The
#: ``attest`` forwarder reads it to keep the old exit code (0 for an answer, 1 for nobody).
LAST_UNSIGNED: dict[str, str] = {}

#: Noun-level verbs and the skill each calls (sub-verbs as "role grant").
NOUN_SKILLS: dict[str, str] = {
    "list": "attest.logbook_list",
    "waiting": "attest.drafts",
    "timeline": "journal.tail",
    "validate": "attest.logbook_validate",
    "anchor": "attest.anchor",
    "due": "attest.obligations",
    "access": "attest.access",
    "verify-all": "attest.verify_all",
    "role grant": "attest.role_grant",
    "role revoke": "attest.role_revoke",
    "role list": "attest.role_list",
    "device enroll": "attest.device_enroll",
    "device reclaim": "attest.device_reclaim",
    "device retire": "attest.device_retire",
    "device list": "attest.device_list",
    "location init": "attest.location_init",
    "location code": "attest.location_code",
}
NOUN_VERBS: tuple[str, ...] = tuple(dict.fromkeys(k.split()[0] for k in NOUN_SKILLS))
_NESTED = {"role": "role_verb", "device": "device_verb", "location": "location_verb"}

#: One log's verbs and the skill each calls.
LOG_SKILLS: dict[str, str] = {
    "about": "attest.logbook_list",
    "show": "attest.show",
    "due": "attest.obligations",
    "verify": "attest.verify",
    "intervals": "attest.intervals",
    "new": "attest.new",
    "sign": "attest.sign",
    "propose": "attest.propose",
    "supplement": "attest.supplement",
    "acknowledge": "attest.ack",
    "cosign": "attest.cosign",
    "unacknowledged": "attest.unacknowledged",
    "cosign-pending": "attest.cosign_pending",
    "access": "attest.access",
    "seal": "attest.seal",
    "seal-preview": "attest.seal_preview",
    "seal-due": "attest.seal_due",
    "export": "attest.export",
    "export-preview": "attest.export_preview",
    "blank-form": "attest.export_blank",
    "import": "attest.import",
    "imports": "attest.imports",
    "import-withdraw": "attest.import_withdraw",
}
#: Verbs that sign (or import) at the prompt and exit 3 when nothing was signed.
_SIGNING = frozenset({"new", "sign"})
#: Verbs whose results are listed by ``next:`` after a result (reads only).
_NEXT_READS = ("show", "due")

OLD_PLATFORM_VERBS: tuple[str, ...] = tuple(sorted(OLD_PLATFORM_WORDS))

assert set(NOUN_VERBS) <= NOUN_LEVEL_WORDS, "every noun-level verb is a reserved word"


# --------------------------------------------------------------------------- names


def brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def _registry():
    from . import registry

    return registry


def _handle_of(logbook_id: str) -> str:
    try:
        book = _registry().get(logbook_id)
    except Exception:  # noqa: BLE001 - an undeclared log is named by its id
        return logbook_id
    return book.handle or book.id


def handles() -> list[str]:
    try:
        return [b.handle or b.id for b in _registry().all_logbooks()]
    except Exception:  # noqa: BLE001 - a broken declaration costs Tab its handles, never the CLI
        return []


# --------------------------------------------------------------------------- the forwarders' clock


def _installed_version() -> tuple[int, int, int] | None:
    try:
        from importlib.metadata import version

        from axiom.infra.manifest import parse_semver

        return parse_semver(version("axiom-os-lm"))
    except Exception:  # noqa: BLE001 - unknown version: the forwarders keep working
        return None


def forwarders_removed() -> bool:
    """Whether this release is at or past ``FORWARDERS_REMOVED_IN``."""
    from axiom.infra.manifest import parse_semver

    have = _installed_version()
    return have is not None and have >= parse_semver(FORWARDERS_REMOVED_IN.lstrip("v"))


def warn_moved(old: str, new: str, *, stream=None) -> None:
    """The one stderr line an old form prints, unless ``AXI_NO_DEPRECATION_WARNING`` is set."""
    if os.environ.get(QUIET_ENV, "").strip() not in ("", "0"):
        return
    cli = brand_cli()
    print(
        f'"{cli} {old}" is now "{cli} {new}"; the old form is removed in {FORWARDERS_REMOVED_IN}',
        file=stream or sys.stderr,
    )


def refuse_removed(old: str, new: str) -> int:
    cli = brand_cli()
    print(f'"{cli} {old}" was removed in {FORWARDERS_REMOVED_IN}; use "{cli} {new}"', file=sys.stderr)
    return 2


# --------------------------------------------------------------------------- parsers


def _site(p: argparse.ArgumentParser) -> None:
    p.add_argument("--site", help="which site's chain; default the bound site")


def _json(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="the skill's result as JSON, nothing else")


def _fields(p: argparse.ArgumentParser) -> None:
    p.add_argument("--field", action="append", dest="fields", metavar="KEY=VALUE", help="a field's value; a reading is readings.<instrument>=<value>")


def _add_noun_verbs(sub: Any) -> None:
    ls = sub.add_parser("list", help="The logs here: who writes, who signs, what each holds")
    _json(ls)
    w = sub.add_parser("waiting", help="What waits for you: drafts proposed for you to sign")
    _site(w)
    w.add_argument("--log", dest="logbook", metavar="LOG", help="only drafts of this log")
    w.add_argument("--origin", help="only drafts from this proposer, e.g. agent")
    w.add_argument("--limit", type=int, default=50)
    _json(w)
    tl = sub.add_parser("timeline", help="The Timeline's last rows, across logs and Platform events; -f follows")
    _timeline_args(tl)
    v = sub.add_parser("validate", help="Check a log declaration file without loading it")
    v.add_argument("path")
    _json(v)
    a = sub.add_parser("anchor", help="Sign a Merkle root over the site's log heads (administration)")
    a.add_argument("--site", help="one site; default every site with a chain")
    _json(a)
    d = sub.add_parser("due", help="What is due and what was missed, in every log")
    _site(d)
    d.add_argument("--tick", action="store_true", help="record and announce what came due or was missed")
    _json(d)
    ac = sub.add_parser("access", help="Who read the logs, when and through which surface")
    ac.add_argument("--since", help="7d, 2h, or an ISO time")
    ac.add_argument("--limit", type=int, default=50)
    _json(ac)
    va = sub.add_parser("verify-all", help="Check every log at every served site from sequence 1")
    _json(va)

    device = sub.add_parser("device", help="Enrolled signing devices (administration)")
    device_sub = device.add_subparsers(dest="device_verb")
    de = device_sub.add_parser("enroll", help="Enrol a device")
    de.add_argument("device_id")
    _site(de)
    de.add_argument("--class", dest="device_class", required=True, choices=["personal", "kiosk", "tablet", "phone"])
    de.add_argument("--location")
    de.add_argument("--mobility", choices=["fixed", "portable"], default="fixed")
    _json(de)
    for verb, text in (("reclaim", "Issue a fresh one-time claim code"), ("retire", "Retire a device")):
        dp = device_sub.add_parser(verb, help=text)
        dp.add_argument("device_id")
        _json(dp)
    dl = device_sub.add_parser("list", help="List enrolled devices")
    _site(dl)
    _json(dl)

    role = sub.add_parser("role", help="Signing roles per site (administration)")
    role_sub = role.add_subparsers(dest="role_verb")
    for verb, text in (("grant", "Give a person a signing role, or the importer role for one log"),
                       ("revoke", "Take a signing role or a log's importer role away")):  # fmt: skip
        rp = role_sub.add_parser(verb, help=text)
        rp.add_argument("principal", help="@name:context")
        rp.add_argument("role", help="a role a log names, e.g. operator; or importer")
        _site(rp)
        rp.add_argument("--log", dest="logbook", metavar="LOG",
                        help="the one log a log role (importer) is held for")  # fmt: skip
        if verb == "grant":
            rp.add_argument("--until", metavar="DATE",
                            help="when a log role ends (YYYY-MM-DD or ISO time); required for importer")  # fmt: skip
        _json(rp)
    rl = role_sub.add_parser("list", help="Who holds which roles at a site")
    _site(rl)
    _json(rl)

    location = sub.add_parser("location", help="Presence locations and their codes")
    location_sub = location.add_subparsers(dest="location_verb")
    li = location_sub.add_parser("init", help="Create a location's secret in the vault")
    li.add_argument("location")
    _site(li)
    _json(li)
    lc = location_sub.add_parser("code", help="Show the location's current code")
    lc.add_argument("location")
    _site(lc)
    _json(lc)


def _timeline_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--lane", action="append", metavar="LANE", help="Only this lane (repeatable)")
    p.add_argument("--run", metavar="ID", help="Only rows of this subject (a run id)")
    p.add_argument("--follow", "-f", action="store_true", help="Keep printing new rows until Ctrl-C")
    p.add_argument("--n", type=int, default=20, metavar="N", help="Rows to show first (default 20)")
    p.add_argument("--mine", action="store_true",
                   help="Only what you did or asked for, and what was done for you")  # fmt: skip
    p.add_argument("--agent", metavar="NAME", help="Only what this agent did (its Activity)")
    p.add_argument("--level", dest="levels", action="append", choices=["ok", "warn", "bad", "info"],
                   help="Only rows at this level (repeatable)")  # fmt: skip


def _export_filters(p: argparse.ArgumentParser, *, formats: bool = True) -> None:
    """An export's formats, forms and filters (unit L6): W3's record filters, and supplements."""
    if formats:
        p.add_argument("--format", action="append", choices=["jsonl", "text", "csv", "pdf"],
                       help="a view in the package (repeatable); pdf prints each --form as a PDF too")  # fmt: skip
        p.add_argument("--form", action="append", help="a declared [[export]] form (repeatable)")
    p.add_argument("--type", dest="entry_type", action="append", help="an entry type (repeatable)")
    p.add_argument("--meaning", action="append", help="a meaning (repeatable)")
    p.add_argument("--signer", help="a signer's handle")
    p.add_argument("--from", dest="from", help="from this time (ISO 8601)")
    p.add_argument("--to", help="up to this time (ISO 8601)")
    p.add_argument("--interval", help="one interval, KIND:NUMBER")
    p.add_argument("-q", dest="q", help="words the entries contain")
    p.add_argument("--no-supplements", dest="include_supplements", action="store_false",
                   default=None, help="leave supplements out")  # fmt: skip


def _add_log_verbs(sub: Any) -> None:
    ab = sub.add_parser("about", help="The log's declaration: entry types, roles, rules, retention")
    _json(ab)
    show = sub.add_parser("show", help="Latest entries, or one entry with its proof")
    show.add_argument("attestation_id", nargs="?", help="one entry's id")
    _site(show)
    show.add_argument("--limit", type=int, default=20)
    show.add_argument("--type", action="append", help="keep this entry type (repeatable)")
    show.add_argument("--signer", help="keep entries signed by this handle, @name:context")
    show.add_argument("--since", help="ISO 8601, inclusive; UTC when no zone is given")
    show.add_argument("--until", help="ISO 8601, inclusive; UTC when no zone is given")
    show.add_argument("--interval", help="keep entries inside one interval, kind:number")
    show.add_argument("--text", help="keep entries whose signed content contains this text")
    _json(show)
    due = sub.add_parser("due", help="What is due in this log, and what was missed")
    _site(due)
    _json(due)
    ver = sub.add_parser("verify", help="Check the log's signed chain end to end")
    _site(ver)
    _json(ver)
    ivs = sub.add_parser("intervals", help="The intervals the log has run, with counts and gaps")
    _site(ivs)
    ivs.add_argument("--kind", help="one declared interval kind")
    _json(ivs)

    new = sub.add_parser("new", help="Write an entry and sign it at the prompt (signs)")
    new.add_argument("type", help="entry type, e.g. ROUND_CHECK")
    _site(new)
    new.add_argument("--meaning", help="what signing means, e.g. performed; default the type's only one")
    new.add_argument("--title", required=True)
    new.add_argument("--body")
    new.add_argument("--occurred-at", dest="occurred_at", metavar="TIME",
                     help="when it happened, if earlier (ISO 8601); recorded-at stays the server's")  # fmt: skip
    _fields(new)
    _json(new)
    sign = sub.add_parser("sign", help="Read a draft proposed for you, complete it, and sign at the prompt (signs)")
    sign.add_argument("draft_id")
    _fields(sign)
    _json(sign)
    prop = sub.add_parser("propose", help="Make a draft for a named person; never signs")
    prop.add_argument("type", help="entry type, e.g. ROUND_CHECK")
    _site(prop)
    prop.add_argument("--meaning", help="what signing means; default the type's only one")
    prop.add_argument("--title", required=True)
    prop.add_argument("--body")
    _fields(prop)
    prop.add_argument("--for", dest="for_principal", required=True, metavar="@NAME:CONTEXT",
                      help="the person who will sign it")  # fmt: skip
    prop.add_argument("--integration", metavar="EXT", help="the installed extension proposing it")
    _json(prop)
    supp = sub.add_parser("supplement", help="Correct, clarify or add to an entry, signed at the prompt (signs)")
    supp.add_argument("attestation_id", help="the signed entry to supplement")
    supp.add_argument("--reason", required=True, choices=("correction", "clarification", "addition"))
    _site(supp)
    supp.add_argument("--title")
    supp.add_argument("--body")
    _fields(supp)
    _json(supp)
    ack = sub.add_parser("acknowledge", help="Say you have read an entry, at the prompt (signs)")
    ack.add_argument("attestation_id")
    _site(ack)
    _json(ack)
    cs = sub.add_parser("cosign", help="Co-sign an entry as the second person its type requires (signs)")
    cs.add_argument("attestation_id")
    _site(cs)
    _json(cs)
    un = sub.add_parser("unacknowledged", help="Entries nobody has acknowledged yet")
    _site(un)
    un.add_argument("--by", help="a person's handle, or `me`: entries they have not acknowledged")
    un.add_argument("--since-seq", dest="since_seq", type=int)
    _json(un)
    cp = sub.add_parser("cosign-pending", help="Entries waiting for a co-signature")
    _site(cp)
    cp.add_argument("--for-roles", dest="for_roles", help="only entries one of these roles may co-sign")
    cp.add_argument("--older-than", dest="older_than", type=int, metavar="SECONDS")
    _json(cp)
    acc = sub.add_parser("access", help="Who read this log, when and through which surface")
    acc.add_argument("--since", help="7d, 2h, or an ISO time")
    acc.add_argument("--limit", type=int, default=50)
    _json(acc)
    for verb, text in (
        ("seal", "Close out a period of the log: sign a summary, its open items and the chain head (signs)"),
        ("seal-preview", "What a close-out of the log's period would state; signs nothing"),
    ):
        sl = sub.add_parser(verb, help=text)
        _site(sl)
        sl.add_argument("--boundary", help="a declared interval kind, or range (default: as declared)")
        if verb == "seal":
            sl.add_argument("--summary", required=True, help="the close-out in your own words")
        sl.add_argument("--from-seq", dest="from_seq", type=int)
        sl.add_argument("--to-seq", dest="to_seq", type=int)
        _json(sl)
    due = sub.add_parser("seal-due", help="Clock periods that ended and are not closed, for you")
    _site(due)
    _json(due)
    exp = sub.add_parser("export", help="Write an evidence package for the log, whole or filtered")
    _site(exp)
    exp.add_argument("--out", help="a new or empty directory")
    _export_filters(exp)
    _json(exp)
    pv = sub.add_parser("export-preview", help="Show at most 200 entries of an export; writes nothing")
    _site(pv)
    pv.add_argument("--format", choices=["text", "csv", "jsonl"], default="text")
    _export_filters(pv, formats=False)
    _json(pv)
    im = sub.add_parser("import", help="Import history from another system as records imported, not signed: a dry run unless --commit")
    im.add_argument("path", metavar="FILE", help="mapped history, JSON Lines, one entry per line")
    _site(im)
    im.add_argument("--commit", metavar="DIGEST", help="write it: the digest the dry run printed (asks at the prompt)")
    im.add_argument("--source", metavar="NAME", help="the system it came from, e.g. paperlog (default: file)")
    _json(im)
    ib = sub.add_parser("imports", help="The log's import batches, and any withdrawal")
    _site(ib)
    _json(ib)
    iw = sub.add_parser("import-withdraw", help="Withdraw a whole import batch with a reason (asks at the prompt)")
    iw.add_argument("batch", help="the batch id `imports` lists")
    iw.add_argument("--reason", required=True, help="why the batch is not to be relied on")
    _site(iw)
    _json(iw)
    bf = sub.add_parser("blank-form", help="Print a declared form blank, for a backup binder; reads no entry")
    bf.add_argument("form", help="the declared form's id")
    bf.add_argument("--pdf", dest="out", metavar="FILE", help="write it as a PDF to a new FILE")
    _site(bf)
    _json(bf)


def noun_parser(prog: str | None = None) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog or f"{brand_cli()} log")
    _add_noun_verbs(p.add_subparsers(dest="verb"))
    return p


def log_parser(handle: str) -> argparse.ArgumentParser:
    """The per-log parser: the same for every log."""
    p = argparse.ArgumentParser(prog=f"{brand_cli()} log {handle}", description=f"The log {handle}.")
    _add_log_verbs(p.add_subparsers(dest="verb"))
    return p


def get_parser(prog: str | None = None) -> argparse.ArgumentParser:
    """Every verb, ``platform``, and one sub-command per log handle: for help and Tab."""
    from axiom.extensions.builtins.log import cli as platform_cli

    prog = prog or f"{brand_cli()} log"
    parser = argparse.ArgumentParser(
        prog=prog,
        description=f"Logs: signed records of what people did. One log's verbs are `{prog} <log> <verb>`.",
    )
    found = handles()
    metavar = "{" + ",".join([*NOUN_VERBS, "platform", *found]) + "}"
    sub = parser.add_subparsers(dest="verb", metavar=metavar)
    _add_noun_verbs(sub)
    plat = sub.add_parser("platform", help="Platform events: the machine's own log (not signed)")
    platform_cli.add_verbs(plat.add_subparsers(dest="platform_verb"))
    for handle in found:
        sub.add_parser(handle, parents=[log_parser(handle)], add_help=False, help=f"The log {handle}")
    return parser


def skill_for_verb() -> dict[str, str]:
    """``SKILL_FOR_VERB`` for the logs declared now (ADR-243)."""
    from axiom.extensions.builtins.log import cli as platform_cli

    out = dict(NOUN_SKILLS)
    out["due --tick"] = "attest.obligations_tick"
    out.update({f"platform {v}": s for v, s in platform_cli.SKILL_FOR_VERB.items()})
    for handle in handles():
        out.update({f"{handle} {v}": s for v, s in LOG_SKILLS.items()})
    return out


def __getattr__(name: str) -> Any:
    if name == "SKILL_FOR_VERB":
        return skill_for_verb()
    raise AttributeError(name)


def parser_sources() -> list[str]:
    """The files this noun's parser is assembled from, so the Tab cache notices a change."""
    import importlib

    out = [os.path.abspath(__file__)]
    for mod in ("axiom.extensions.builtins.log.cli", "axiom.extensions.builtins.attest.logbooks"):
        out.append(os.path.abspath(importlib.import_module(mod).__file__))
    try:
        out += [os.path.abspath(b.source) for b in _registry().all_logbooks() if b.source]
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------- running a skill


class _Unanswered:
    """A prompt that records when nobody answered (end of input, or interrupted)."""

    def __init__(self, prompt):
        self.prompt = prompt
        self.missed = False

    def __call__(self, text: str) -> str:
        try:
            return self.prompt(text)
        except (EOFError, KeyboardInterrupt):
            self.missed = True
            return ""


def _terminal_prompt(prompt: str) -> str:
    return input(prompt)


def _build_ctx() -> SkillContext:
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.principal import resolve_principal

    from . import skills as attest_skills

    return SkillContext(
        registry=attest_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.log"),
        user_prompt=_terminal_prompt if sys.stdin.isatty() else None,
        principal=resolve_principal(),
    )


def _params(args: argparse.Namespace, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    skip = {"verb", "json", "tick", *_NESTED.values(), *drop}
    return {k: v for k, v in vars(args).items() if k not in skip}


def _invoke(name: str, params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    return invoke_capability(ctx.registry, name, params, ctx, surface=CLI_SURFACE)


def _sole_meaning(logbook_id: str, type_id: str) -> str | None:
    try:
        meanings = _registry().get(logbook_id).type(type_id).meanings
    except Exception:  # noqa: BLE001 - the skill names the unknown type
        return None
    return meanings[0] if len(meanings) == 1 else None


# --------------------------------------------------------------------------- printing


def _print_text(result: SkillResult) -> None:
    v: Any = result.value
    if not isinstance(v, dict):
        return
    if "decisions" in v and "digest" in v:
        _print_import(v)
        return
    if "batches" in v:
        if not v["batches"]:
            print(f"nothing has been imported into {_handle_of(v['logbook'])}")
        for b in v["batches"]:
            gone = f"  withdrawn by {b['withdrawn']['by']}: {b['withdrawn']['reason']}" if b["withdrawn"] else ""
            print(f"{b['batch']}  {b['entries']} entries  #{b['first_seq']}-#{b['last_seq']}  "
                  f"{b['earliest'][:10]} to {b['latest'][:10]}  by {b['imported_by']}{gone}")  # fmt: skip
        return
    if "batch" in v and "written" in v:
        if v["written"]:
            print(f"imported {v['written']} entries as batch {v['batch']} (imported, not signed); "
                  f"{v['skipped']} already on the chain")  # fmt: skip
        else:
            print(f"nothing imported: all {v['skipped']} entries are already on the chain")
        return
    if "withdrawn" in v and "record" in v:
        print(f"withdrew import batch {v['batch']}: {v['withdrawn']} entries, record #{v['record']['seq']}")
        return
    if "logbooks" in v:
        if not v["logbooks"]:
            print("no logs are declared on this node")
        for b in v["logbooks"]:
            print(f"{b.get('handle') or b['id']}  {b['display']}  v{b['version']}  (floor {b['posture']})")
            for t in b["types"]:
                print(f"  {t['id']}: {', '.join(t['meanings'])}; roles {', '.join(t['roles'])}")
    elif "drafts" in v:
        if not v["drafts"]:
            print("nothing is waiting for you")
        for d in v["drafts"]:
            held = "  held" if d.get("held") else ""
            title = (d.get("content") or {}).get("title") or ""
            print(f"{_handle_of(d['logbook'])}  draft {d['draft_id']}  {d['entry_type']}  {title}  "
                  f"proposed by {d['origin']}  {d['created_at']}{held}")  # fmt: skip
        if v["drafts"]:
            d = v["drafts"][0]
            print(f"complete and sign one with: {brand_cli()} log {_handle_of(d['logbook'])} sign DRAFT_ID")
    elif "draft_id" in v and v.get("status") == "open" and "origin" in v:
        print(f"proposed {v['entry_type']} in the log {_handle_of(v['logbook'])} for "
              f"{v['for_principal']} as {v['origin']}: draft {v['draft_id']}")  # fmt: skip
    elif "covers" in v and "open_items" in v:
        c = v["covers"]
        print(f"{v['title']}: #{c['from_seq']} to #{c['to_seq']}, chain head #{c['head_seq']} {c['head_digest'][:8]}")
        if not v["open_items"]:
            print("  nothing is open")
        for i in v["open_items"]:
            print(f"  open: {i['summary']}")
    elif "records" in v:
        if "by" in v and not v["records"]:
            print("every entry is acknowledged" + (f" by {v['by']}" if v.get("by") else ""))
        for r in v["records"]:
            if r.get("cosign") == "pending":
                roles = ", ".join(r.get("cosign_roles") or ["the declared role"])
                print(f"#{r['seq']:>5}  waiting for co-signature by {roles}")
            print(f"#{r['seq']:>5}  {r['occurred_at']}  {r['entry_type'] or ''} "
                  f"{r['meaning']}  {r['title'] or ''}  by {r['signer']}")  # fmt: skip
    elif "record" in v:
        print(json.dumps(v["record"], indent=2, ensure_ascii=False))
        from .changes import block

        moved = block(v.get("changes") or [])
        if moved:
            print(moved)
    elif v.get("status") == "signed":
        print(f"signed #{v['seq']} in the log {_handle_of(v['logbook'])} at {v['site_id']}\n  {v['uri']}")
        if v.get("anchor_id"):
            print(f"  anchored the site: {v['anchor_id']}")
    elif v.get("status") in ("hold", "ask"):
        print(f"{v['status']}: nothing signed; draft {v['draft_id']} stays open")
    elif "anchors" in v:
        for a in v["anchors"]:
            print(f"anchored {a['site_id']}: {a['logbooks']} logs, root {a['root']}")
    elif "checked" in v:
        if v["ok"]:
            print(f"Chain intact: {v['checked']} records; head #{v['head_seq']} {v['head_digest']}")
        else:
            print(f"BROKEN at #{v['first_bad_seq']}: {v['reason']} ({v['checked']} verified before it)")
        a = v.get("anchor")
        if a is None:
            print("  no anchor covers this log yet")
        elif a["ok"]:
            print(f"  anchor {a['anchor_id']} ({a['created_at']}) matches")
        else:
            print(f"  anchor {a['anchor_id']} ({a['created_at']}) FAILS: {a['reason']}")
    elif "obligations" in v:
        if not v["obligations"]:
            print("nothing is owed: no interval with an obligation is open")
        for o in v["obligations"]:
            mark = {"ok": "✓", "warn": "⏳", "missed": "✕"}.get(o["state"], "?")
            print(f"{mark} {o['state'].upper():<6} {o['type']} in {o['interval']['kind']} "
                  f"{o['interval']['number']}  due {o['due_at']}  ({o['severity']})")  # fmt: skip
    elif "assignments" in v:
        if not v["assignments"]:
            print(f"nobody holds a signing role at {v['site_id']}")
        for a in v["assignments"]:
            print(f"{a['principal']}  {', '.join(a['roles'])}")
        for a in v.get("log_roles") or []:
            print(f"{a['principal']}  {a['role']} for {a['logbook']} until {a['until']}")
    elif "granted" in v:
        scope = f" for {v['logbook']} until {v['until']}" if v.get("logbook") else ""
        print(f"granted {v['granted']} to {v['principal']} at {v['site_id']}{scope}")
    elif "revoked" in v:
        print(f"revoked {v['revoked']} from {v['principal']} at {v['site_id']}")
    elif "devices" in v:
        if not v["devices"]:
            print("no devices are enrolled at this site")
        for d in v["devices"]:
            print(f"{d['device_id']}  {d['device_class']}  {d['location'] or '-'}  {d['mobility']}")
    elif "claim" in v and "device" not in v:
        c = v["claim"]
        print(f"claim code: {c['code']}  (one use, {c['expires_in_minutes']} min)\n  {c['how']}")
    elif "device" in v:
        d = v["device"]
        print(f"enrolled {d['device_id']} as {d['device_class']} at {d['location'] or 'no location'} ({d['mobility']})")
        c = v.get("claim")
        if c:
            print(f"claim code: {c['code']}  (one use, {c['expires_in_minutes']} min)\n  {c['how']}")
    elif "code" in v:
        print(f"{v['location']}: {v['code']}  (changes every {v['window_seconds']}s)")
    elif "export_id" in v:
        where = f" to {v['out']}" if v.get("out") else ""
        print(f"evidence package {v['export_id']}: {v['counts']['selected']} of {v['counts']['chain']} "
              f"records{where}\n  check with: {v['verify']}")  # fmt: skip
    elif "truncated" in v and "text" in v:
        print(v["text"], end="")
        if v["truncated"]:
            print(f"(the first {v['records']} of {v['selected']} records)")
    elif "out" in v:
        print(f"wrote {v['records']} records to {v['out']}\n  check with: {v['verify']}")
    elif "types" in v:
        print(f"valid: {v['id']} v{v['version']} ({len(v['types'])} types)")
        if v.get("not_yet_enforced"):
            print("  declared, not yet enforced: " + ", ".join(v["not_yet_enforced"]))


def _print_import(v: dict[str, Any]) -> None:
    """A dry run: the counts, the warnings, each line's decision, the digest and the next step."""
    c = v["counts"]
    if v.get("status") == "not_imported":
        print("not imported: nothing was written")
    print(f"dry run of an import into {_handle_of(v['logbook'])}: {c['lines']} lines, {c['new']} new, "
          f"{c['skipped']} already on the chain, {c['errors']} errors, {c['warnings']} warnings")  # fmt: skip
    print(f"  source's own chain: {v['source_verified']}")
    for w in v["warnings"]:
        print(f"  warning, line {w['line']} ({w['source_ref']}): {w['warning']}")
    for d in v["decisions"]:
        print(f"  line {d['line']:>4}  {d['action']:<5}  {d['source_ref']}  {d['reason']}")
    print(f"  digest {v['digest']}")
    if c["errors"]:
        print("  fix the errors and run the dry run again; nothing can be imported until then")
    elif c["new"] and v.get("status") != "not_imported":
        print(f"  import it with: {brand_cli()} log {_handle_of(v['logbook'])} import FILE --commit {v['digest']}")


def _emit(result: SkillResult, as_json: bool, *, next_for: str | None = None) -> int:
    if as_json:
        print(json.dumps({"ok": result.ok, "value": result.value, "errors": result.errors},
                         indent=2, default=str, ensure_ascii=False))  # fmt: skip
    else:
        _print_text(result)
        for e in result.errors or []:
            print(f"error: {e}", file=sys.stderr)
        if next_for and result.ok:
            cli = brand_cli()
            print("next: " + " · ".join(f"{cli} log {next_for} {v}" for v in _NEXT_READS))
    return 0 if result.ok else 1


# --------------------------------------------------------------------------- dispatch


def _platform(argv: list[str]) -> int:
    from axiom.extensions.builtins.log import cli as platform_cli

    return platform_cli.main(argv)


def _timeline(argv: list[str]) -> int:
    """``log timeline``: the Timeline's Tail through ``journal.tail`` (unit J3), which prints the
    rows itself; refused (a lane the person may not read) exits 1."""
    from axiom.extensions.builtins.journal.skills import bind_default as journal_skills
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.principal import resolve_principal

    p = argparse.ArgumentParser(prog=f"{brand_cli()} log timeline")
    _timeline_args(p)
    args = p.parse_args(argv)
    ctx = SkillContext(
        registry=journal_skills(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.log.timeline"),
        principal=resolve_principal(),
    )
    params = {"lanes": args.lane, "run": args.run, "limit": args.n, "follow": args.follow,
              "mine": args.mine, "agent": args.agent, "levels": args.levels}  # fmt: skip
    result = _invoke("journal.tail", params, ctx)
    for e in result.errors or []:
        print(f"error: {e}", file=sys.stderr)
    return 0 if result.ok else 1


def _noun(argv: list[str]) -> int:
    parser = noun_parser()
    args = parser.parse_args(argv)
    nested = _NESTED.get(args.verb)
    if nested and getattr(args, nested) is None:
        parser.parse_args([args.verb, "--help"])
        return 2
    path = f"{args.verb} {getattr(args, nested)}" if nested else args.verb
    name = "attest.obligations_tick" if args.verb == "due" and args.tick else NOUN_SKILLS[path]
    ctx = _build_ctx()
    return _emit(_invoke(name, _params(args), ctx), args.json)


def _per_log(book: Any, argv: list[str]) -> int:
    handle = book.handle or book.id
    parser = log_parser(handle)
    if not argv:
        parser.print_help()
        return 2
    args = parser.parse_args(argv)
    if args.verb is None:
        parser.print_help()
        return 2
    name = LOG_SKILLS[args.verb]
    params = _params(args)
    ctx = _build_ctx()
    spec = ctx.registry.spec(name)
    if spec is None or "logbook" in (spec.inputs or {}):
        params["logbook"] = book.id
    if args.verb in ("new", "propose") and not params.get("meaning"):
        params["meaning"] = _sole_meaning(book.id, params["type"])
        if params["meaning"] is None:
            print(f"{parser.prog} {args.verb}: give --meaning; {params['type']} has more than one",
                  file=sys.stderr)  # fmt: skip
            return 2
    if args.verb in _SIGNING:
        return _sign(name, params, ctx, args.json, handle)
    return _emit(_invoke(name, params, ctx), args.json, next_for=handle if not args.json else None)


def _sign(name: str, params: dict[str, Any], ctx: SkillContext, as_json: bool, handle: str) -> int:
    """``new`` and ``sign``: 0 signed, 3 nothing signed (held, asked, nobody answered), 1 refused."""
    guard = _Unanswered(ctx.user_prompt) if ctx.user_prompt is not None else None
    if guard is not None:
        ctx = replace(ctx, user_prompt=guard)
    result = _invoke(name, params, ctx)
    rc = _emit(result, as_json, next_for=None if as_json else handle)
    nobody = guard is None or guard.missed
    if result.ok and isinstance(result.value, dict) and result.value.get("status") in ("hold", "ask"):
        LAST_UNSIGNED["why"] = "answered"
        return NOTHING_SIGNED
    if not result.ok and nobody:
        LAST_UNSIGNED["why"] = "nobody"
        return NOTHING_SIGNED
    return rc


def _unknown(word: str, rest: list[str]) -> int:
    """Exit 2: an id typed where a handle belongs names the handle; anything else, the nearest."""
    cli = brand_cli()
    known = handles()
    for b in _logbooks_safe():
        if b.id == word and (b.handle or b.id) != word:
            again = " ".join([cli, "log", b.handle, *rest])
            print(f'"{word}" is the id of a log; name it by its handle: "{again}"', file=sys.stderr)
            return 2
    near = difflib.get_close_matches(word, [*known, *NOUN_VERBS, "platform"], n=1)
    hint = f'; did you mean "{near[0]}"?' if near else ""
    logs = ", ".join(known) if known else "none are declared"
    print(f'{cli} log: no log or verb "{word}"{hint} (logs: {logs})', file=sys.stderr)
    return 2


def _logbooks_safe() -> list[Any]:
    try:
        return _registry().all_logbooks()
    except Exception:  # noqa: BLE001
        return []


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        get_parser().print_help(sys.stdout if argv else sys.stderr)
        return 0 if argv else 2
    word, rest = argv[0], argv[1:]
    if word == "platform":
        return _platform(rest)
    if word == "timeline":
        try:
            return _timeline(rest)
        except SystemExit as exc:
            return int(exc.code or 0)
    if word == "tail" and any(t in ("--lane", "--run") or t.startswith(("--lane=", "--run=")) for t in rest):
        # J3 put the Timeline's Tail on `log tail --lane`; its form is `log timeline`.
        if forwarders_removed():
            return refuse_removed("log tail --lane", "log timeline --lane")
        warn_moved("log tail --lane", "log timeline --lane")
        try:
            return _timeline(rest)
        except SystemExit as exc:
            return int(exc.code or 0)
    if word in OLD_PLATFORM_VERBS:
        if forwarders_removed():
            return refuse_removed(f"log {word}", f"log platform {word}")
        warn_moved(f"log {word}", f"log platform {word}")
        return _platform(argv)
    if word in NOUN_VERBS:
        try:
            return _noun(argv)
        except SystemExit as exc:  # argparse: usage
            return int(exc.code or 0)
    if word in RESERVED_HANDLES:
        print(f'{brand_cli()} log {word}: not available in this release', file=sys.stderr)
        return 2
    try:
        book = _registry().by_handle(word)
    except Exception as exc:  # noqa: BLE001 - a broken declaration is a refusal, with its reason
        print(f"{brand_cli()} log: {exc}", file=sys.stderr)
        return 1
    if book is None:
        return _unknown(word, rest)
    try:
        return _per_log(book, rest)
    except SystemExit as exc:
        return int(exc.code or 0)


__all__ = [
    "FORWARDERS_REMOVED_IN",
    "LOG_SKILLS",
    "NOTHING_SIGNED",
    "NOUN_SKILLS",
    "NOUN_VERBS",
    "OLD_PLATFORM_VERBS",
    "QUIET_ENV",
    "forwarders_removed",
    "get_parser",
    "log_parser",
    "main",
    "parser_sources",
    "refuse_removed",
    "warn_moved",
]
