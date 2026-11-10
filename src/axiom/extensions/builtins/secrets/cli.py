# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi secrets``: the deprecated spelling of ``axi vault`` (ADR-218).

Every old verb still works. Each one is mapped onto its ``vault`` form, which
then runs, and one line on stderr names that form so scripts and habits can
move before the alias goes in v0.72.0. ``AXI_NO_DEPRECATION_WARNING=1`` hides
the line.

    secrets diagnose|exposed|rotate|set|list|rm|get    -> vault <same verb>
    secrets reindex|discover|annotate                  -> vault <same verb>
    secrets git-credential|wire-git                    -> vault <same verb>
    secrets audit                                      -> vault audit --expiry
    secrets vault [--backend ...]                      -> vault where [...]
    secrets vault setup [--shell ...]                  -> vault setup [...]

``secrets audit`` keeps its old exit code: it was a report that never failed,
so the alias asks ``vault audit`` for the same (``--report-only``).
"""

from __future__ import annotations

import os
import sys

#: Old verb -> the ``vault`` argv that replaces it. ``vault`` is handled apart
#: because ``secrets vault setup`` and ``secrets vault`` became two verbs.
VERB_MAP: dict[str, tuple[str, ...]] = {
    "diagnose": ("diagnose",),
    "exposed": ("exposed",),
    "rotate": ("rotate",),
    "set": ("set",),
    "list": ("list",),
    "rm": ("rm",),
    "get": ("get",),
    "audit": ("audit", "--expiry"),
    "reindex": ("reindex",),
    "discover": ("discover",),
    "git-credential": ("git-credential",),
    "wire-git": ("wire-git",),
    "annotate": ("annotate",),
    "vault": ("where",),
    "vault setup": ("setup",),
}

#: What ``secrets vault`` accepted, split by the verb each flag now belongs to.
_WHERE_VALUED = frozenset({"--backend", "--url", "--token-file", "--mount"})
_SETUP_VALUED = frozenset({"--shell", "--container", "--port"})
_SETUP_SWITCHES = frozenset({"--all-detected", "--yes"})

QUIET_ENV = "AXI_NO_DEPRECATION_WARNING"


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def _split_vault(rest: list[str]) -> tuple[str, list[str]]:
    """``secrets vault ...`` -> ("vault" | "vault setup", the flags that verb takes).

    The old verb took every flag on one parser and ignored the ones the other
    half used, so each flag goes to the verb it belongs to. ``--json``,
    ``--help`` and anything unknown go to whichever verb runs, so its parser
    reports an unknown flag exactly as before.
    """
    setup = False
    where_args: list[str] = []
    setup_args: list[str] = []
    common: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        flag, eq, _ = tok.partition("=")
        if tok == "setup":
            setup = True
        elif flag in _WHERE_VALUED or flag in _SETUP_VALUED:
            bucket = where_args if flag in _WHERE_VALUED else setup_args
            bucket.append(tok)
            if not eq and i + 1 < len(rest):
                bucket.append(rest[i + 1])
                i += 1
        elif tok in _SETUP_SWITCHES:
            setup_args.append(tok)
        else:
            common.append(tok)
        i += 1
    return ("vault setup", setup_args + common) if setup else ("vault", where_args + common)


def translate(argv: list[str]) -> tuple[list[str], str | None, str | None]:
    """``axi secrets`` argv -> (``axi vault`` argv, the old verb, its new spelling).

    Options before the verb (``--json``, ``--help``) are kept in place. An
    unknown verb is passed through, so ``vault``'s own parser rejects it with
    its usual message.
    """
    argv = list(argv)
    lead: list[str] = []
    while argv and argv[0].startswith("-"):
        lead.append(argv.pop(0))
    if not argv:
        return lead, None, None
    verb, rest = argv[0], argv[1:]
    if verb == "vault":
        old, rest = _split_vault(rest)
    elif verb == "audit":
        old = verb
        rest = [*rest, "--report-only"]
    else:
        old = verb
    new = VERB_MAP.get(old)
    if new is None:
        return [*lead, verb, *rest], None, None
    return [*lead, *new, *rest], old, " ".join(new)


def warn(old: str, new: str, *, stream=None) -> None:
    """One line on stderr naming the new form, unless ``AXI_NO_DEPRECATION_WARNING`` is set."""
    if os.environ.get(QUIET_ENV, "").strip() not in ("", "0"):
        return
    from axiom.extensions.builtins.vault.skills import CLI_ALIAS_REMOVED_IN

    cli = _brand_cli()
    print(
        f"{cli} secrets {old}: deprecated, use `{cli} vault {new}` "
        f"(removed in {CLI_ALIAS_REMOVED_IN}; {QUIET_ENV}=1 hides this line)",
        file=stream or sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    from axiom.extensions.builtins.vault import cli as vault_cli

    new_argv, old, new = translate(list(sys.argv[1:] if argv is None else argv))
    if old is not None and new is not None:
        warn(old, new)
    elif not new_argv or new_argv in (["-h"], ["--help"]):
        print(f"{_brand_cli()} secrets is now {_brand_cli()} vault.", file=sys.stderr)
    return vault_cli.main(new_argv)


__all__ = ["QUIET_ENV", "VERB_MAP", "main", "translate", "warn"]


if __name__ == "__main__":
    raise SystemExit(main())
