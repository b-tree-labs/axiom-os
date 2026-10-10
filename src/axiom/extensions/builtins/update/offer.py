# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Offer an available update: announce it durably, then ask.

Two properties make this an improvement rather than a nag.

It is announced through the notification channel, so it reaches the person
wherever they read alerts instead of only in whichever terminal was open. That
is the durable `notifications alert` path, and it is keyed by version so a
release is announced once. An update notice that reappears every launch is the
thing people mute, and a muted channel costs more than a missed release.

And the question is answerable with Enter. Taking the update is the common
answer and should cost one keystroke. Declining should be equally cheap, and an
answer nobody gave should not install software — so an unrecognised reply
re-asks rather than falling through to the default, and the prompt is only put
to a terminal with a person at it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

#: (choice, label). Order is the order offered; the first is the Enter default.
UPGRADE_CHOICES: tuple[tuple[str, str], ...] = (
    ("upgrade", "Yes, upgrade now  [Enter]"),
    ("not_now", "Not now — ask again next time  [n]"),
    ("skip_version", "Skip this version  [s]"),
)

_ANSWERS: dict[str, str] = {
    "": "upgrade",
    "y": "upgrade", "yes": "upgrade", "1": "upgrade",
    "n": "not_now", "no": "not_now", "2": "not_now",
    "s": "skip_version", "skip": "skip_version", "3": "skip_version",
}


def resolve_choice(answer: str | None) -> str | None:
    """Map a typed answer to a choice, or None when it is not one.

    Empty (a bare Enter) is ``upgrade``. Anything unrecognised returns None so
    the caller re-asks: a stray keystroke must not install software.
    """
    if answer is None:
        return None
    return _ANSWERS.get(answer.strip().lower())


def announce_update(
    *,
    product: str,
    current: str,
    available: str,
    notes: str,
    recipient: str,
    send: Callable[..., Any] | None = None,
) -> None:
    """Post a durable, per-version alert that an update is available.

    Never raises: this runs at startup, and a notification backend that is down
    must not stop the CLI.
    """
    summary = f"{product} {available} is available (you are on {current})"
    dispatch = send or _default_send
    try:
        dispatch(
            recipient=recipient,
            summary=summary,
            body=notes or "",
            actor="@update:local",
            priority="low",
            # Keyed to the version so the release is announced once rather
            # than on every launch.
            dedup_key=f"update-available:{product}:{available}",
        )
    except Exception:  # noqa: BLE001 - announcing must not break startup
        _log.debug("update announcement failed", exc_info=True)


def _default_send(**kwargs: Any) -> Any:
    """The durable inbox path, resolved lazily so tests need no database."""
    from axiom.extensions.builtins.notifications.inbox_db import DatabaseInboxStore

    body = kwargs.pop("body", "")
    summary = kwargs.pop("summary", "")
    return DatabaseInboxStore().write_alert(
        summary=f"{summary}\n\n{body}".strip() if body else summary, **kwargs
    )


def offer_update_interactively(
    *,
    product: str,
    current: str,
    available: str,
    repo: str = "",
    recipient: str = "@cli:local",
    ask: Callable[[str], str] | None = None,
    run_update: Callable[[], Any] | None = None,
) -> str:
    """Announce the update, show what changed, and ask. Returns the choice.

    Called only once the guards have passed. Announcing happens either way, so
    the release is in the person's inbox even if they decline here — the
    terminal is where they are now, the inbox is where they will look later.

    Never raises: this sits in front of a chat session starting.
    """
    from axiom.extensions.builtins.update.release_notes import (
        fetch_releases,
        format_update_notice_for,
        package_repo,
        releases_between,
    )

    try:
        # The official release notes of every release being taken, from the
        # repository the installed package names for itself: someone several
        # releases behind is deciding about all of them, not the newest only.
        if not repo:
            from axiom.infra.branding import get_branding

            repo = package_repo(get_branding().package_name or "axiom-os-lm")
        releases = releases_between(fetch_releases(repo), current=current, available=available)
        notice = format_update_notice_for(
            product=product, current=current, available=available, releases=releases, repo=repo
        )
        notes = notice
        announce_update(
            product=product, current=current, available=available,
            notes=notes, recipient=recipient,
        )
        prompt = ask or _default_ask
        print(f"\n{notice}\n")
        for index, (_, label) in enumerate(UPGRADE_CHOICES, 1):
            print(f"  {index}. {label}")

        # Re-ask rather than defaulting: an answer nobody meant must not
        # install software. Bounded so a stuck terminal cannot trap a session.
        for _ in range(3):
            choice = resolve_choice(prompt("\nUpgrade now? [Y/n/s] "))
            if choice:
                break
        else:
            return "not_now"

        if choice == "skip_version":
            skip_version(available)
        elif choice == "upgrade" and run_update is not None:
            run_update()
        return choice
    except (EOFError, KeyboardInterrupt):
        # Ctrl-C or a closed stdin is a decline, not a crash.
        return "not_now"
    except Exception:  # noqa: BLE001 - never block a session from starting
        _log.debug("update offer failed", exc_info=True)
        return "not_now"


def _default_ask(prompt: str) -> str:
    return input(prompt)


def should_offer_update(
    *, is_newer: bool, interactive: bool, disabled: bool
) -> bool:
    """Whether to put the question to a person at all.

    The prompt is the part that can annoy, so the guards matter more than the
    feature. No terminal means nobody is there to answer — a pipe, a serving
    surface, a subagent, CI — and a question asked into one of those either
    hangs or is answered by accident.
    """
    return bool(is_newer) and bool(interactive) and not bool(disabled)


def _skip_file(state_dir: Path | None = None) -> Path:
    if state_dir is None:
        from axiom.infra.paths import get_project_state_dir

        state_dir = get_project_state_dir()
    return Path(state_dir) / "skipped-versions.json"


def is_version_skipped(version: str, *, state_dir: Path | None = None) -> bool:
    """Whether the user already declined this specific version.

    Fails toward asking: an unreadable file must not mean "skip everything
    forever", which is silence that looks like agreement.
    """
    try:
        path = _skip_file(state_dir)
        if not path.exists():
            return False
        return version in (json.loads(path.read_text(encoding="utf-8")) or [])
    except Exception:  # noqa: BLE001 - corrupt or unreadable
        return False


def skip_version(version: str, *, state_dir: Path | None = None) -> None:
    """Remember that this version was declined.

    Keyed by version, so a decline is about this release rather than about
    ever being told again. Never raises.
    """
    try:
        path = _skip_file(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) or []
        except Exception:  # noqa: BLE001 - start clean on a corrupt file
            existing = []
        if version not in existing:
            existing.append(version)
        path.write_text(json.dumps(existing), encoding="utf-8")
    except Exception:  # noqa: BLE001 - declining must not fail the CLI
        _log.debug("could not record skipped version", exc_info=True)


# -- at every interactive entry point ------------------------------------------

#: Never interrupted: the update itself, and asking for help or the version.
NEVER_ASK = frozenset({"update", "help", "-h", "--help", "--version", "-V", "version"})


def offer_at_entry(
    argv: list[str],
    *,
    interactive: bool,
    info: Any = None,
    editable: bool | None = None,
    state_dir: Path | None = None,
    today: Any = None,
) -> str:
    """Offer a newer release before the command the person typed, once a day.

    Returns what happened: ``none`` (current), ``notice`` (say so and run on:
    no person, a checkout, help, or switched off), ``asked_today`` (declined
    already today), or the choice made. An upgrade re-runs ``argv`` on the new
    code; it does not return.
    """
    import os
    from datetime import date as _date

    if info is None:
        from axiom.extensions.builtins.update.version_check import VersionChecker

        info = VersionChecker().check_remote_version(timeout=3.0)
    if not getattr(info, "is_newer", False):
        return "none"
    disabled = os.environ.get("AXIOM_DISABLE_UPDATE_NUDGE") == "1"
    if disabled or not interactive or not argv or argv[0] in NEVER_ASK:
        return "notice"
    if editable is None:
        editable = _running_from_a_checkout()
    if editable:
        return "notice"
    available = str(info.available or "")
    if not available or is_version_skipped(available, state_dir=state_dir):
        return "notice"
    day = (today or _date.today()).isoformat()
    asked = _asked_file(state_dir)
    try:
        last = json.loads(asked.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable means ask
        last = {}
    if last.get("date") == day and last.get("version") == available:
        return "asked_today"
    try:
        asked.parent.mkdir(parents=True, exist_ok=True)
        asked.write_text(json.dumps({"date": day, "version": available}), encoding="utf-8")
    except OSError:
        pass  # forgetting means asking again tomorrow, never failing the command
    from axiom.infra.branding import get_branding

    return offer_update_interactively(
        product=get_branding().product_name,
        current=str(info.current),
        available=available,
        repo=os.environ.get("AXIOM_CONSUMER_RELEASE_REPO", "").replace("https://github.com/", ""),
        run_update=lambda: _update_then_resume(argv),
    )


def _asked_file(state_dir: Path | None) -> Path:
    if state_dir is None:
        from axiom.infra.paths import get_project_state_dir

        state_dir = get_project_state_dir()
    return Path(state_dir) / "update-asked.json"


def _running_from_a_checkout() -> bool:
    """A source checkout updates with git, not pip; it is told, not asked."""
    import importlib.metadata as md

    from axiom.infra.branding import get_branding

    package = get_branding().package_name or "axiom-os-lm"
    try:
        raw = md.distribution(package).read_text("direct_url.json") or "{}"
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except Exception:  # noqa: BLE001 - no install record: running from source
        return True


def _update_then_resume(argv: list[str]) -> None:
    """Update this environment, then run the person's command on the new code.

    Without the re-run, the command would still execute on the old code already
    loaded in this process, and the person would meet the same fault again.
    """
    import os
    import sys

    from axiom.extensions.builtins.update.cli import main as update_main

    if update_main([]) not in (0, None):
        print("  The update did not complete; running your command on the current version.\n")
        return
    print("  Updated. Running your command on the new version.\n")
    sys.stdout.flush()
    if os.name == "nt":  # no exec on Windows: run it and hand back its result
        import subprocess

        sys.exit(subprocess.run([sys.argv[0], *argv], check=False).returncode)
    os.execv(sys.argv[0], [sys.argv[0], *argv])


__all__ = [
    "NEVER_ASK",
    "UPGRADE_CHOICES",
    "announce_update",
    "is_version_skipped",
    "offer_at_entry",
    "resolve_choice",
    "should_offer_update",
    "skip_version",
]
