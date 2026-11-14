# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``<cli> attest``: the old spelling of ``<cli> log`` (ADR-243), hidden from help.

Every old verb and flag set is mapped onto its ``log`` form (prd-attestation §6b, "Moving
from today's commands"), one stderr line names that form, and the new form runs:

    attest logbook list|validate FILE          -> log list | log validate FILE
    attest new --logbook L --type T ...        -> log L new T ...
    attest sign DRAFT                          -> log L sign DRAFT  (L read from the draft)
    attest show [ID] --logbook L               -> log L show [ID]
    attest verify --logbook L | --all          -> log L verify | log verify-all
    attest obligations [--logbook L]           -> log L due | log due
    attest export --logbook L --out DIR        -> log L export --out DIR
    attest anchor|device|role|location ...     -> log anchor|device|role|location ...
    attest drafts [--logbook L]                -> log waiting [--log L]
    attest propose --logbook L --type T ...    -> log L propose T ...
    attest supplement|ack|cosign ID            -> log L supplement|acknowledge|cosign ID
    attest intervals|unacknowledged|cosign-pending|access|seal|seal-preview|export-preview --logbook L
                                               -> log L <same verb>

``--logbook <id>`` becomes the log's handle. Exit codes and ``--json`` are the new form's,
except that ``new`` and ``sign`` keep their old codes when nothing was signed: 0 for an answer
of hold or ask, 1 for nobody at the prompt (the new form says 3 for both).
``AXI_NO_DEPRECATION_WARNING=1`` hides the line; the forms are removed in
``log_cli.FORWARDERS_REMOVED_IN``.
"""

from __future__ import annotations

import sys

from . import log_cli

#: Verbs that name one log with ``--logbook`` and keep their name under ``log <log>``.
_PER_LOG_SAME = ("show", "export", "intervals", "unacknowledged", "cosign-pending", "seal", "seal-preview",
                 "export-preview")
#: Verbs that name an entry by id; the log is the entry's.
_BY_RECORD = {"supplement": "supplement", "ack": "acknowledge", "cosign": "cosign"}
_NOUN_SAME = ("anchor", "device", "role", "location")


def __getattr__(name: str):
    if name == "FORWARDERS_REMOVED_IN":
        return log_cli.FORWARDERS_REMOVED_IN
    raise AttributeError(name)


def _take(rest: list[str], flag: str) -> tuple[str | None, list[str]]:
    """``--flag VALUE`` or ``--flag=VALUE`` out of ``rest``: (value or None, what is left)."""
    out: list[str] = []
    value = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == flag and i + 1 < len(rest):
            value = rest[i + 1]
            i += 2
            continue
        if tok.startswith(flag + "="):
            value = tok.split("=", 1)[1]
        else:
            out.append(tok)
        i += 1
    return value, out


def _handle(logbook_id: str) -> str:
    return log_cli._handle_of(logbook_id)


def _log_of_draft(draft_id: str) -> str | None:
    from . import service

    try:
        return service.draft(draft_id)["logbook"]
    except Exception:  # noqa: BLE001 - an unknown draft: the old form is a usage error
        return None


def _log_of_record(attestation_id: str) -> str | None:
    from . import service

    try:
        rec = service.record(attestation_id)
    except Exception:  # noqa: BLE001
        return None
    return rec["logbook"] if rec else None


def _first_positional(rest: list[str]) -> str | None:
    return next((t for t in rest if not t.startswith("-")), None)


def translate(argv: list[str]) -> list[str] | None:
    """An old ``attest`` argv as its ``log`` argv, or None when it has no new form."""
    if not argv:
        return None
    verb, rest = argv[0], list(argv[1:])
    if verb == "logbook":
        if rest and rest[0] in ("list", "validate"):
            return [rest[0], *rest[1:]]
        return None
    if verb in _NOUN_SAME:
        return [verb, *rest]
    if verb == "sign":
        draft = _first_positional(rest)
        lid = _log_of_draft(draft) if draft else None
        return [_handle(lid), "sign", *rest] if lid else None
    if verb in _BY_RECORD:
        rid = _first_positional(rest)
        lid = _log_of_record(rid) if rid else None
        return [_handle(lid), _BY_RECORD[verb], *rest] if lid else None
    logbook, rest = _take(rest, "--logbook")
    if verb == "drafts":
        return ["waiting", *(["--log", _handle(logbook)] if logbook else []), *rest]
    if verb == "verify":
        if "--all" in rest:
            return ["verify-all", *[t for t in rest if t != "--all"]]
        return [_handle(logbook), "verify", *rest] if logbook else None
    if verb == "obligations":
        return [_handle(logbook), "due", *rest] if logbook else ["due", *rest]
    if verb == "access":
        return [_handle(logbook), "access", *rest] if logbook else ["access", *rest]
    if verb in ("new", "propose"):
        etype, rest = _take(rest, "--type")
        if not logbook or not etype:
            return None
        return [_handle(logbook), verb, etype, *rest]
    if verb == "show" and not logbook:
        rid = _first_positional(rest)
        lid = _log_of_record(rid) if rid else None
        return [_handle(lid), "show", *rest] if lid else None
    if verb in _PER_LOG_SAME:
        return [_handle(logbook), verb, *rest] if logbook else None
    return None


def _words(new: list[str]) -> str:
    """The new form's command words, without its arguments: ``log demo verify``."""
    head = new[0]
    if head in log_cli.NOUN_VERBS:
        sub = [new[1]] if head in ("role", "device", "location") and len(new) > 1 else []
        return " ".join(["log", head, *sub])
    return " ".join(["log", *new[:2]])


def _old_words(argv: list[str]) -> str:
    if argv[0] in ("logbook", "role", "device", "location") and len(argv) > 1:
        return f"attest {argv[0]} {argv[1]}"
    return f"attest {argv[0]}"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cli = log_cli.brand_cli()
    if not argv or argv[0] in ("-h", "--help"):
        print(f'"{cli} attest" is now "{cli} log"; see "{cli} log --help"', file=sys.stderr)
        return 0 if argv else 2
    new = translate(argv)
    if new is None:
        print(f'{cli} attest {argv[0]}: no such form; the logs are under "{cli} log" '
              f'(see "{cli} log --help")', file=sys.stderr)  # fmt: skip
        return 2
    old, now = _old_words(argv), _words(new)
    if log_cli.forwarders_removed():
        return log_cli.refuse_removed(old, now)
    log_cli.warn_moved(old, now)
    log_cli.LAST_UNSIGNED.clear()
    rc = log_cli.main(new)
    if argv[0] in ("new", "sign") and rc == log_cli.NOTHING_SIGNED:
        # the old form's code: an answer of hold or ask was 0, nobody at the prompt was 1
        return 0 if log_cli.LAST_UNSIGNED.get("why") == "answered" else 1
    return rc


__all__ = ["main", "translate"]
