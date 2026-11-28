# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Held pull requests in Needs you, for the person who has to act (#2246, item 3).

The heartbeat records each watched repo's held pull requests (``record``), read
only, at most every fifteen minutes. Needs you reads the record (``for_person``)
and lists each one under To approve for the person who acts: the requested
reviewer when there is one, otherwise an approver. Approval itself happens on
GitHub, so the item links there and carries no action.

A person is matched to a GitHub login by principal (``@login:github``) or by an
alias on the node's identity, in the same form (``@login:github``). An email-like
form (``login@github.com``) is refused: it reads as an address that does not exist. The
local terminal caller (``@cli:local``) is the node's owner.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.release.lander import verdict as V

log = logging.getLogger(__name__)

#: How often the heartbeat refreshes one repo's record.
REFRESH = timedelta(minutes=15)
#: A record older than this is not shown: a stopped heartbeat leaves no stale asks.
STALE = timedelta(hours=24)

_LOCAL_CALLERS = frozenset({"@cli:local"})
_PRINCIPAL = re.compile(r"^@([A-Za-z0-9][A-Za-z0-9-]*):github$")


def record_path(state_dir: Path, repo: str) -> Path:
    return Path(state_dir) / "agents" / "rivet" / "held" / (repo.replace("/", "__") + ".json")


def _login(text: str) -> str:
    text = (text or "").strip()
    found = _PRINCIPAL.match(text)
    return found.group(1) if found else ""


def _owner_identity() -> dict[str, Any]:
    try:
        from axiom.vega.federation.identity import _DEFAULT_KEYS_DIR

        data = json.loads((_DEFAULT_KEYS_DIR / "identity.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - no identity: only principals that name a login match
        return {}


def logins_for(person: str, *, identity: dict[str, Any] | None = None) -> set[str]:
    """The GitHub logins this person answers for."""
    direct = _login(person)
    if direct:
        return {direct}
    identity = _owner_identity() if identity is None else identity
    owner = str(identity.get("owner") or "")
    aliases = [str(a) for a in identity.get("aliases") or ()]
    if not owner or (person != owner and person not in aliases and person not in _LOCAL_CALLERS):
        return set()
    return {login for login in (_login(a) for a in aliases) if login}


def _who(verdict: V.Verdict, approvers: Iterable[str]) -> list[str]:
    login = _login(f"@{verdict.who}:github") if verdict.who and " " not in verdict.who else ""
    if login:
        return [login]
    return [x for x in (_login(a) for a in approvers) if x]


def record(
    state_dir: Path,
    repo: str,
    report: dict,
    *,
    approvers: Iterable[str] = (),
    now: datetime | None = None,
) -> Path:
    """Write ``repo``'s held pull requests: only those a person acts on, never drafts."""
    approvers = tuple(approvers)
    items = []
    for verdict in V.all_verdicts(report):
        if verdict.state != "person" or verdict.draft or verdict.who == "the author":
            # The author's own step (a conflict, changes asked of them) is not a decision to queue.
            continue
        reviewer = bool(_login(f"@{verdict.who}:github")) if " " not in verdict.who else False
        items.append(
            {
                "verb": "Review" if reviewer else "Approve",
                "number": verdict.number,
                "title": verdict.title,
                "why": " ".join(verdict.why),
                "next": verdict.next_steps[0] if verdict.next_steps else "",
                "who": _who(verdict, approvers),
                "url": f"https://github.com/{repo}/pull/{verdict.number}",
            }
        )
    path = record_path(state_dir, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"repo": repo, "at": (now or datetime.now(UTC)).isoformat(), "items": items}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def _at(path: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["at"])
    except Exception:  # noqa: BLE001 - unreadable: treated as absent
        return None


def due(state_dir: Path, repo: str, *, now: datetime | None = None) -> bool:
    """True when ``repo``'s record is missing or older than REFRESH."""
    at = _at(record_path(state_dir, repo))
    return at is None or (now or datetime.now(UTC)) - at >= REFRESH


def refresh(state_dir: Path, repos: Iterable[str], *, now: datetime | None = None) -> list[str]:
    """Heartbeat hook: rebuild each due repo's record from a read-only report. Returns the repos read."""
    from axiom.extensions.builtins.release.lander.adapters import GitHubForge
    from axiom.extensions.builtins.release.lander.config import load_config
    from axiom.extensions.builtins.release.lander.report import build_report

    done = []
    for repo in repos:
        if not due(state_dir, repo, now=now):
            continue
        try:
            config = load_config(Path(state_dir), repo)
            pulls = GitHubForge(limit=config.pr_limit, self_landers=config.self_landers).list_open(
                repo
            )
            report = build_report(pulls, repo=repo, config=config, samples=[1])
            approvers = config.approvers or tuple(
                f"@{x}:github" for x in sorted(logins_for("@cli:local"))
            )
            record(state_dir, repo, report.to_dict(), approvers=approvers, now=now)
            done.append(repo)
        except Exception as exc:  # noqa: BLE001 - one repo failing never stops the heartbeat
            log.warning("rivet: held pull requests for %s could not be read: %s", repo, exc)
    return done


def for_person(
    person: str, *, state_dir: Path | None = None, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Needs you items: each held pull request this person acts on, under To approve."""
    logins = logins_for(person)
    if not logins:
        return []
    if state_dir is None:
        from axiom.infra.paths import get_user_state_dir

        state_dir = get_user_state_dir()
    now = now or datetime.now(UTC)
    folder = Path(state_dir) / "agents" / "rivet" / "held"
    out: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            at = datetime.fromisoformat(data["at"])
        except Exception:  # noqa: BLE001 - one unreadable record never empties the rest
            continue
        if now - at > STALE:
            continue
        repo = str(data.get("repo") or "")
        for item in data.get("items") or ():
            if not logins & set(item.get("who") or ()):
                continue
            out.append(
                {
                    "id": f"rivet:{repo}#{item['number']}",
                    "section": "approve",
                    "kind": "pull_request",
                    "title": f"{item.get('verb') or 'Approve'} #{item['number']} in {repo}: {item.get('title') or ''}".rstrip(
                        ": "
                    ),
                    "source": "rivet",
                    "due": None,
                    "priority": "normal",
                    "href": item.get("url"),
                    "action": None,
                    "reason": item.get("why") or "",
                    "next": item.get("next") or "",
                    "created_at": data.get("at"),
                }
            )
    return out


__all__ = [
    "REFRESH",
    "STALE",
    "due",
    "for_person",
    "logins_for",
    "record",
    "record_path",
    "refresh",
]
