# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Whether a person has been asked to review a pull request.

A pending request to a human user, and a human review that asks for
changes, both block landing. A team request is not a person: this
repository does not use CODEOWNERS to ask for review, so a team
assignment is not treated as an explicit human request. Bot and app
accounts are not people either.
"""

from __future__ import annotations

from typing import Any

_BOT_TYPES = {"bot", "mannequin", "botuser", "app"}


def _typename(item: dict[str, Any]) -> str:
    return str(item.get("__typename") or item.get("type") or "").strip()


def _login(item: dict[str, Any]) -> str:
    login = item.get("login")
    if login:
        return str(login)
    author = item.get("author")
    if isinstance(author, dict):
        return str(author.get("login") or "")
    if isinstance(author, str):
        return author
    return ""


def is_human(login: str, typename: str = "") -> bool:
    """True for a user account. Teams, bots, and app accounts are not."""
    if not login or not str(login).strip():
        return False
    kind = typename.strip().lower()
    if kind in _BOT_TYPES or kind == "team":
        return False
    lowered = str(login).strip().lower()
    if lowered.endswith("[bot]") or lowered.endswith("-bot"):
        return False
    return True


def pending_human_reviewers(raw: object) -> tuple[str, ...]:
    """Logins with an open review request. Teams and bots are omitted."""
    if not isinstance(raw, list):
        return ()
    found: list[str] = []
    for item in raw:
        if isinstance(item, str):
            if is_human(item):
                found.append(item)
            continue
        if not isinstance(item, dict):
            continue
        kind = _typename(item)
        if kind.lower() == "team":
            continue
        login = _login(item)
        if is_human(login, kind):
            found.append(login)
    return tuple(dict.fromkeys(found))


def same_login(left: str, right: str) -> bool:
    """GitHub logins, with an optional leading ``@``, compared ignoring case."""
    def norm(value: str) -> str:
        text = str(value or "").strip().lower()
        if text.startswith("@"):
            text = text[1:]
        return text

    return bool(norm(left)) and norm(left) == norm(right)


def _commit_sha(item: dict[str, Any]) -> str:
    commit = item.get("commit")
    if isinstance(commit, str):
        return commit
    if isinstance(commit, dict):
        return str(commit.get("oid") or commit.get("sha") or "")
    return str(item.get("commitOid") or item.get("commit_sha") or "")


def _review_rows(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def allows_self_approval(pr_author: str, self_landers: tuple[str, ...] = ()) -> bool:
    """True when this author's own approval is configured to count."""
    return any(same_login(pr_author, login) for login in self_landers)


def changes_requested_by(
    raw: object,
    *,
    pr_author: str = "",
    self_landers: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Human authors whose latest review asks for changes.

    ``raw`` is oldest-first, or already one review per author. The last
    state for each login wins, so a later approval clears an earlier
    request for changes. The pull-request author's own approval clears
    theirs only when the author is in ``self_landers``.
    """
    return _latest(raw, pr_author, self_landers)[1]


def review_approvals(
    raw: object,
    *,
    pr_author: str = "",
    self_landers: tuple[str, ...] = (),
) -> tuple[tuple[str, str], ...]:
    """``(login, commit sha)`` for a latest APPROVED review that counts.

    The author's own approval is included only when the author is in
    ``self_landers``. An approval with no commit sha is omitted.
    """
    return _latest(raw, pr_author, self_landers)[0]


def _latest(
    raw: object,
    pr_author: str,
    self_landers: tuple[str, ...] = (),
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    latest_state: dict[str, str] = {}
    latest_sha: dict[str, str] = {}
    for item in _review_rows(raw):
        author = item.get("author")
        typename = _typename(author) if isinstance(author, dict) else ""
        login = _login(item)
        if not is_human(login, typename):
            continue
        state = str(item.get("state") or "").upper().replace(" ", "_")
        if not state:
            continue
        if (
            state == "APPROVED"
            and same_login(login, pr_author)
            and not allows_self_approval(pr_author, self_landers)
        ):
            continue
        latest_state[login] = state
        latest_sha[login] = _commit_sha(item)
    approvals = tuple(
        (login, latest_sha[login])
        for login, state in latest_state.items()
        if state == "APPROVED" and latest_sha[login]
    )
    changes = tuple(login for login, state in latest_state.items() if state == "CHANGES_REQUESTED")
    return approvals, changes


def review_hold(pending: tuple[str, ...], changes: tuple[str, ...]) -> str:
    """The comment line that blocks landing. Empty when nothing blocks."""
    parts: list[str] = []
    if changes:
        parts.append("changes requested by " + ", ".join(changes))
    if pending:
        parts.append("waiting on " + ", ".join(pending))
    return "; ".join(parts)


def _row_author(row: dict[str, Any]) -> str:
    author = row.get("author")
    if isinstance(author, dict):
        return str(author.get("login") or "")
    return str(author or "")


def _review_list(row: dict[str, Any]) -> object:
    reviews = row.get("latestReviews")
    if not isinstance(reviews, list) or not reviews:
        reviews = row.get("reviews") or row.get("latest_reviews") or ()
    return reviews


def review_fields(
    row: dict[str, Any],
    *,
    self_landers: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Pending reviewers and humans who asked for changes.

    The pull-request author's own approval counts only when the author
    is in ``self_landers``. Otherwise it neither clears a
    changes-requested review nor answers a review request for them.
    """
    author = _row_author(row)
    pending = pending_human_reviewers(row.get("reviewRequests") or row.get("review_requests"))
    approvals, changes = _latest(_review_list(row), author, self_landers)
    if author and allows_self_approval(author, self_landers):
        if any(same_login(login, author) for login, _sha in approvals):
            pending = tuple(login for login in pending if not same_login(login, author))
    return pending, changes


def hold_for_pull(pull: object) -> str:
    """The hold for an OpenPull. Missing fields mean there is no hold."""
    pending = tuple(getattr(pull, "pending_human_reviewers", ()) or ())
    changes = tuple(getattr(pull, "changes_requested_by", ()) or ())
    return review_hold(pending, changes)
