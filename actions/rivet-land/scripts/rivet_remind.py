#!/usr/bin/env python3
"""Tell the owner when RIVET holds or escalates a pull request.

The lander records an escalation on the runner. That file is gone when the
job ends. This wrapper keeps the queue in GitHub: one sticky comment on the
pull request, the ``rivet:waiting`` label, and one issue named
``RIVET: needs <owner>``. A later run finds the comment by its marker.

``RELEASE_FIX_WEBHOOK_URL`` and ``RELEASE_FIX_WEBHOOK_KEY`` are read by the
lander and are unset. This script does not create them.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Set by configure(). The owner is the one GitHub login who gets the "needs"
# issue and whose /rivet commands are honoured. Empty means nobody is.
ASSIGNEE = ""
ISSUE_TITLE = "RIVET: needs owner"
MARKER = "<!-- rivet-needs-owner -->"
_STATE = re.compile(r"<!-- rivet-needs-owner-state (.*?) -->", re.S)


def _state_prefix(marker: str) -> str:
    return marker.removesuffix("-->").rstrip() + "-state"


def configure(*, owner: str, issue_title: str = "", marker: str = "") -> None:
    """Set the owner, issue title and sticky marker for this run."""
    global ASSIGNEE, ISSUE_TITLE, MARKER, _STATE
    ASSIGNEE = owner.strip()
    ISSUE_TITLE = issue_title.strip() or f"RIVET: needs {ASSIGNEE or 'owner'}"
    MARKER = marker.strip() or "<!-- rivet-needs-owner -->"
    _STATE = re.compile(re.escape(_state_prefix(MARKER)) + r" (.*?) -->", re.S)


_VERBS = {"merge", "option", "hold", "rerun", "close"}
_VIEW_FIELDS = ",".join(
    (
        "number",
        "title",
        "headRefName",
        "headRefOid",
        "baseRefName",
        "author",
        "labels",
        "updatedAt",
        "createdAt",
        "isDraft",
        "mergeable",
        "url",
        "body",
        "additions",
        "deletions",
        "files",
        "statusCheckRollup",
        "reviewRequests",
        "reviews",
        "latestReviews",
        "comments",
        "state",
    )
)


@dataclass(frozen=True)
class Cmd:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class CheckView:
    name: str
    conclusion: str
    url: str
    run_id: str


@dataclass(frozen=True)
class CommentView:
    body: str
    author: str
    created_at: datetime
    comment_id: int | None = None


@dataclass(frozen=True)
class PullSnapshot:
    number: int
    title: str
    body: str
    url: str
    labels: tuple[str, ...]
    files: tuple[str, ...]
    created_at: datetime
    head_sha: str
    author: str
    checks: tuple[CheckView, ...]
    comments: tuple[CommentView, ...]


@dataclass(frozen=True)
class Outcome:
    status: str
    reason: str


@dataclass(frozen=True)
class WaitingRow:
    number: int
    title: str
    url: str
    waited_seconds: int
    urgent: bool
    blocking: str


@dataclass(frozen=True)
class Option:
    key: str
    title: str
    tradeoff: str
    action: str


@dataclass(frozen=True)
class Decision:
    sticky: str | None
    fresh: str | None
    rerun_run_ids: tuple[str, ...]
    escalate: bool
    check: dict | None
    add_labels: tuple[str, ...]
    remove_labels: tuple[str, ...]


@dataclass(frozen=True)
class ReminderConfig:
    assignee: str
    issue_title: str
    sticky_marker: str
    waiting_label: str
    remind_after_hours: int
    urgent_remind_after_hours: int
    urgent_labels: tuple[str, ...]
    urgent_words: tuple[re.Pattern[str], ...]
    urgent_paths: tuple[re.Pattern[str], ...]
    rerun_check_names: tuple[str, ...]
    check_name: str
    claim_label: str
    claim_ttl_s: int
    foreign_claim_labels: tuple[str, ...] = ()
    rerun_cancelled: bool = True


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _parse_time(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    return _aware(datetime.fromisoformat(text))


def author_allowed(login: str, allowed: str | None = None) -> bool:
    """Only the owner. With no owner configured nobody is allowed."""
    who = (ASSIGNEE if allowed is None else allowed).strip().lower()
    return bool(who) and login.strip().lower() == who


def parse_command(body: str) -> tuple[str, str] | None:
    """First ``/rivet`` line. Unknown verbs and trailing words are ignored."""
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("/rivet"):
            continue
        parts = stripped.split()
        if len(parts) < 2 or parts[0].lower() != "/rivet":
            continue
        verb = parts[1].lower()
        if verb not in _VERBS:
            return None
        if verb == "hold":
            reason = stripped.split(maxsplit=2)[2].strip() if len(parts) >= 3 else ""
            return ("hold", reason)
        if verb == "option":
            if len(parts) != 3:
                return None
            return ("option", parts[2])
        if len(parts) != 2:
            return None
        return (verb, "")
    return None


def encode_state(state: dict) -> str:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return f"{_state_prefix(MARKER)} {payload} -->"


def parse_state(body: str) -> dict:
    match = _STATE.search(body or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def state_from_comments(comments: tuple[CommentView, ...]) -> dict:
    for comment in comments:
        if MARKER in comment.body:
            found = parse_state(comment.body)
            if found:
                return found
    return {}


def _sentences(body: str) -> list[str]:
    text = re.sub(r"<!--.*?-->", " ", body or "", flags=re.S)
    parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    return [part.strip() for part in parts if part.strip()][:2]


def refresher(title: str, body: str, reason: str) -> str:
    """Two or three lines: what the pull request is, and why it is blocked."""
    lines = [title.strip() or "This pull request."]
    lines.extend(_sentences(body)[:1])
    why = f"It is blocked because {reason}." if reason else "It is waiting on a decision."
    if why not in lines:
        lines.append(why)
    return "\n".join(lines[:3])


def options_for(reason: str, *, rerunning: tuple[str, ...]) -> tuple[Option, ...]:
    if rerunning:
        names = ", ".join(rerunning)
        return (
            Option(
                "1",
                "Wait for the rerun",
                f"Reran {names}. A stale failure clears when its cause is gone. The diff stays as it is.",
                "rerun",
            ),
            Option(
                "2",
                "Hold",
                "Records a Hold comment and do-not-merge so the lander cannot merge it while you decide.",
                "hold",
            ),
            Option(
                "3",
                "Close",
                "Closes the pull request without merging. The branch is left in place.",
                "close",
            ),
        )
    if "do-not-merge" in reason:
        return (
            Option(
                "1",
                "Keep the hold",
                "The pull request stays unmerged until the Releases line is satisfied. It will not land by accident.",
                "hold",
            ),
            Option(
                "2",
                "Merge",
                "Lands it now. Use this only when the hold reason is already resolved.",
                "merge",
            ),
            Option(
                "3",
                "Close",
                "Closes the pull request without merging. The branch is left in place.",
                "close",
            ),
        )
    if reason.startswith("protected"):
        return (
            Option(
                "1",
                "Merge",
                "You merge this protected change yourself. The lander will not merge it.",
                "merge",
            ),
            Option(
                "2",
                "Hold",
                "Records why it stays open. A protected path stays out of the auto-land path.",
                "hold",
            ),
            Option(
                "3",
                "Close",
                "Closes it if this change should not ship. Nothing is merged.",
                "close",
            ),
        )
    return (
        Option(
            "1",
            "Rerun failed checks",
            "Runs the failed jobs again. It spends CI minutes and does not fix a real test break.",
            "rerun",
        ),
        Option(
            "2",
            "Hold",
            "Stops the lander until you remove do-not-merge. The failure stays visible.",
            "hold",
        ),
        Option(
            "3",
            "Close",
            "Closes the pull request without merging so the work can be folded elsewhere.",
            "close",
        ),
    )


def _recommend(options: tuple[Option, ...], reason: str, rerunning: tuple[str, ...]) -> Option:
    if rerunning or "do-not-merge" in reason:
        return options[0]
    if reason.startswith("protected") and ("security" in reason or "pricing" in reason):
        return options[1]
    if reason.startswith("protected"):
        return options[0]
    return options[0]


def sticky_body(
    *,
    pull: PullSnapshot,
    reason: str,
    now: datetime,
    state: dict,
    urgent: bool,
    rerunning: tuple[str, ...],
    skip_reran: tuple[str, ...] = (),
) -> str:
    """One comment a later run can find. The marker is the identity.

    ``skip_reran`` names reruns tracked per head SHA (cancelled checks) instead of
    once per pull request.
    """
    del urgent
    options = options_for(reason, rerunning=rerunning)
    choice = _recommend(options, reason, rerunning)
    waiting_since = str(state.get("waiting_since") or now.isoformat())
    reran = list(
        dict.fromkeys([*(state.get("reran") or []), *(n for n in rerunning if n not in skip_reran)])
    )
    saved = {
        "waiting_since": waiting_since,
        "last_reminded": str(state.get("last_reminded") or now.isoformat()),
        "options": {item.key: item.action for item in options},
        "reran": reran,
    }
    if state.get("cancel_reran"):
        saved["cancel_reran"] = state["cancel_reran"]
    lines = [
        MARKER,
        encode_state(saved),
        "",
        "## Rivet: needs a decision",
        "",
        refresher(pull.title, pull.body, reason),
        "",
        f"**Blocking:** {reason}",
        "",
        "**Options:**",
        "",
    ]
    for item in options:
        lines.append(f"{item.key}. **{item.title}.** {item.tradeoff}")
    lines.extend(
        [
            "",
            f"**Recommendation:** {choice.key}. {choice.title}. {choice.tradeoff}",
            "",
            "Reply with one of these:",
            "",
            "- `/rivet merge`",
            "- `/rivet option <X>`",
            "- `/rivet hold <reason>`",
            "- `/rivet rerun`",
            "- `/rivet close`",
            "",
        ]
    )
    return "\n".join(lines)


def fresh_remind(pull: PullSnapshot, reason: str, now: datetime) -> str:
    """A new comment. It mentions the owner and does not carry the sticky marker."""
    del now
    return "\n".join(
        [
            f"@{ASSIGNEE} this pull request is still waiting.",
            "",
            refresher(pull.title, pull.body, reason),
            "",
            f"Blocking: {reason}",
            "",
            "Reply with `/rivet merge`, `/rivet option <X>`, `/rivet hold <reason>`, "
            "`/rivet rerun`, or `/rivet close`.",
            "",
        ]
    )


def remind_due(
    state: dict,
    comments: tuple[CommentView, ...],
    now: datetime,
    *,
    hours: int,
    assignee: str,
) -> bool:
    raw = str(state.get("last_reminded") or "")
    if not raw:
        return False
    anchor = _parse_time(raw)
    for comment in comments:
        if comment.author.lower() != assignee.lower() or MARKER in comment.body:
            continue
        if _aware(comment.created_at) > anchor:
            anchor = _aware(comment.created_at)
    return _aware(now) - anchor >= timedelta(hours=hours)


def _word_pattern(word: str) -> re.Pattern[str]:
    if word == "prod":
        return re.compile(r"\bprod(?:uction)?\b", re.I)
    return re.compile(rf"\b{re.escape(word)}\b", re.I)


def is_urgent(pull: PullSnapshot, config: ReminderConfig) -> bool:
    labels = {item.lower() for item in pull.labels}
    if labels & {item.lower() for item in config.urgent_labels}:
        return True
    blob = "\n".join((pull.title, pull.body, *pull.files))
    if any(pattern.search(blob) for pattern in config.urgent_words):
        return True
    return any(pattern.search(path) for path in pull.files for pattern in config.urgent_paths)


def format_wait(seconds: int) -> str:
    remaining = max(0, int(seconds))
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes = remaining // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def sort_waiting(rows: list[WaitingRow]) -> list[WaitingRow]:
    """Urgent pull requests first, and the longest wait first inside that group."""
    return sorted(rows, key=lambda row: (not row.urgent, -row.waited_seconds, row.number))


def issue_body(rows: list[WaitingRow], *, now: datetime) -> str:
    del now
    lines = [
        f"{_state_prefix(MARKER).removesuffix('-state')}-issue -->",
        "",
        f"# {ISSUE_TITLE}",
        "",
        "Pull requests waiting on a decision, most urgent first.",
        "",
    ]
    if not rows:
        lines.extend(["No pull requests are waiting.", ""])
        return "\n".join(lines)
    lines.extend(
        [
            "| PR | Waited | Urgent | Blocking |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        title = row.title.replace("|", "\\|")
        reason = row.blocking.replace("|", "\\|")
        flag = "yes" if row.urgent else "no"
        waited = format_wait(row.waited_seconds)
        lines.append(f"| [#{row.number} {title}]({row.url}) | {waited} | {flag} | {reason} |")
    lines.append("")
    return "\n".join(lines)


def check_request(*, name: str, sha: str, held: bool, summary: str) -> dict:
    text = " ".join(summary.split())
    if not text or text.lower() in {"success", "ok"}:
        raise ValueError("refusing an empty Rivet check summary")
    conclusion = "neutral" if held else "success"
    title = "Held" if held else "Clear"
    return {
        "name": name,
        "head_sha": sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {"title": title, "summary": text},
    }


def claim_is_leaked(
    events: list[dict],
    *,
    now: datetime,
    ttl_s: int,
    closed: bool,
    label: str = "rivet:claimed",
) -> bool:
    """A claim with no live timestamp, or older than the TTL, is a leak."""
    if closed:
        return True
    times = [
        event["created_at"]
        for event in events
        if event.get("event") == "labeled" and event.get("label") == label
    ]
    if not times:
        return True
    latest = max(_aware(moment) for moment in times)
    return (_aware(now) - latest).total_seconds() > ttl_s


def run_id_from_url(url: str) -> str:
    match = re.search(r"/actions/runs/(\d+)", url or "")
    return match.group(1) if match else ""


def rerun_targets(
    checks: tuple[CheckView, ...],
    patterns: tuple[str, ...],
    already: set[str],
) -> list[CheckView]:
    """Failed checks whose cause may have cleared, each rerun at most once per pull request.

    The names come from rerun_check_names (a gate whose failure may clear on
    its own, such as a batching gate or a flaky shared-database suite).
    """
    found: list[CheckView] = []
    for check in checks:
        conclusion = check.conclusion.upper()
        if conclusion not in {"FAILURE", "CANCELLED"}:
            continue
        if check.name in already or not check.run_id:
            continue
        if any(pattern.lower() in check.name.lower() for pattern in patterns):
            found.append(check)
    return found


def cancelled_targets(
    checks: tuple[CheckView, ...],
    required: frozenset[str] | set[str],
    state: dict,
    head_sha: str,
) -> list[CheckView]:
    """Required checks whose latest conclusion is CANCELLED, each rerun once per head SHA.

    A cancelled required check is not a failure of the change: a superseded run, a
    runner lost, a concurrency group. Rerun it before the pull request is treated as failing.
    """
    done = state.get("cancel_reran") or {}
    seen = set(done.get("names") or []) if done.get("sha") == head_sha else set()
    return [
        check
        for check in checks
        if check.conclusion.upper() == "CANCELLED"
        and check.name in required
        and check.run_id
        and check.name not in seen
    ]


def needs_owner(status: str, reason: str, foreign: tuple[str, ...] = ()) -> bool:
    """True when the owner has something to decide.

    ``foreign`` are the claims.foreign_labels from the config: another agent's
    claim is not the owner's to decide, except the human hold, do-not-merge.
    """
    if status == "landed":
        return False
    if "do-not-merge" not in reason and any(
        label != "do-not-merge" and label in reason for label in foreign
    ):
        return False
    if status == "escalated" or "do-not-merge" in reason or reason.startswith("protected"):
        return True
    if status == "waiting" and (
        reason.startswith("waiting on") or reason.startswith("changes requested")
    ):
        return True
    return False


def pull_read_plan(event: str, focus: int | None) -> str:
    """A pull-request event reads that pull request. A schedule reads the open set."""
    if event in {"schedule", "workflow_dispatch", "workflow_run"}:
        return "list"
    if event in {"pull_request", "issue_comment", "pull_request_review"}:
        return "view"
    return "list" if focus is None else "view"


def decide_pull(
    *,
    pull: PullSnapshot,
    outcome: Outcome,
    now: datetime,
    config: ReminderConfig,
    required_cancelled: frozenset[str] | set[str] = frozenset(),
) -> Decision:
    state = state_from_comments(pull.comments)
    interested = needs_owner(outcome.status, outcome.reason, config.foreign_claim_labels)
    targets: list[CheckView] = []
    if interested:
        targets = rerun_targets(
            pull.checks, config.rerun_check_names, set(state.get("reran") or [])
        )
    cancelled: list[CheckView] = []
    if config.rerun_cancelled:
        cancelled = [
            item
            for item in cancelled_targets(pull.checks, required_cancelled, state, pull.head_sha)
            if item.run_id not in {t.run_id for t in targets}
        ]
    if cancelled:
        previous = state.get("cancel_reran") or {}
        names_done = (
            set(previous.get("names") or []) if previous.get("sha") == pull.head_sha else set()
        )
        state = {
            **state,
            "cancel_reran": {
                "sha": pull.head_sha,
                "names": sorted(names_done | {item.name for item in cancelled}),
            },
        }
        targets = [*targets, *cancelled]
    if targets:
        names = tuple(item.name for item in targets)
        sticky = sticky_body(
            pull=pull,
            reason=outcome.reason or "checks not green",
            now=now,
            state=state,
            urgent=is_urgent(pull, config),
            rerunning=names,
            skip_reran=tuple(item.name for item in cancelled),
        )
        summary = (
            f"Reran {', '.join(names)} before treating the pull request as failing. "
            f"{outcome.reason or 'checks are failing'}."
        )
        return Decision(
            sticky=sticky,
            fresh=None,
            rerun_run_ids=tuple(item.run_id for item in targets),
            escalate=False,
            check=check_request(
                name=config.check_name, sha=pull.head_sha, held=True, summary=summary
            ),
            add_labels=(config.waiting_label,),
            remove_labels=(),
        )
    if not interested:
        return Decision(None, None, (), False, None, (), (config.waiting_label,))
    urgent = is_urgent(pull, config)
    hours = config.urgent_remind_after_hours if urgent else config.remind_after_hours
    fresh = None
    posted = dict(state)
    if remind_due(state, pull.comments, now, hours=hours, assignee=config.assignee):
        fresh = fresh_remind(pull, outcome.reason, now)
        posted["last_reminded"] = now.isoformat()
    sticky = sticky_body(
        pull=pull,
        reason=outcome.reason,
        now=now,
        state=posted,
        urgent=urgent,
        rerunning=(),
    )
    summary = f"Held: {outcome.reason}. The Rivet comment lists the options and a recommendation."
    return Decision(
        sticky=sticky,
        fresh=fresh,
        rerun_run_ids=(),
        escalate=True,
        check=check_request(name=config.check_name, sha=pull.head_sha, held=True, summary=summary),
        add_labels=(config.waiting_label,),
        remove_labels=(),
    )


def command_plan(
    command: tuple[str, str],
    *,
    author: str,
    number: int,
    repo: str,
    state: dict,
    run_ids: tuple[str, ...] = (),
) -> list[list[str]]:
    """Argv for ``gh``. Only the owner gets a plan."""
    if not author_allowed(author):
        return []
    verb, arg = command
    if verb == "option":
        mapped = str((state.get("options") or {}).get(arg) or "")
        if mapped not in _VERBS - {"option"}:
            mapped = arg if arg in _VERBS - {"option"} else ""
        if not mapped:
            return [
                [
                    "pr",
                    "comment",
                    str(number),
                    "--repo",
                    repo,
                    "--body",
                    f"Unknown Rivet option {arg}.",
                ]
            ]
        verb = mapped
        if verb == "hold":
            arg = f"{ASSIGNEE} selected hold from the RIVET comment."
    if verb == "merge":
        return [["pr", "merge", str(number), "--repo", repo, "--squash"]]
    if verb == "close":
        return [
            [
                "pr",
                "close",
                str(number),
                "--repo",
                repo,
                "--comment",
                "Closed by /rivet close.",
            ]
        ]
    if verb == "rerun":
        if run_ids:
            return [["run", "rerun", run_id, "--repo", repo, "--failed"] for run_id in run_ids]
        return [["run", "rerun", "--failed"]]
    if verb == "hold":
        if not arg.strip():
            return [
                [
                    "pr",
                    "comment",
                    str(number),
                    "--repo",
                    repo,
                    "--body",
                    "A /rivet hold needs a reason.",
                ]
            ]
        comment = f"Hold: {arg.strip()}\nReleases: {ASSIGNEE}\n"
        return [
            ["pr", "edit", str(number), "--repo", repo, "--add-label", "do-not-merge"],
            ["pr", "comment", str(number), "--repo", repo, "--body", comment],
        ]
    return []


def rate_limited(result: Cmd) -> bool:
    if result.returncode == 0:
        return False
    text = f"{result.stderr}\n{result.stdout}"
    return bool(re.search(r"rate limit|HTTP 429|secondary rate", text, re.I))


def retry_seconds(text: str, attempt: int) -> float:
    base = min(60.0, 2.0 * (2**attempt))
    match = re.search(r"retry after (\d+)", text, re.I)
    if match:
        return max(float(match.group(1)), base)
    return base


class GhClient:
    """``gh`` with backoff when GitHub asks us to slow down."""

    def __init__(self, run, sleep, attempts: int = 5) -> None:
        self._run = run
        self._sleep = sleep
        self._attempts = attempts

    def __call__(self, args: list[str]) -> Cmd:
        last = Cmd(1, "", "no attempt")
        for attempt in range(self._attempts):
            last = self._run(args)
            if not rate_limited(last) or attempt + 1 == self._attempts:
                return last
            detail = f"{last.stderr}\n{last.stdout}"
            self._sleep(retry_seconds(detail, attempt))
        return last


def _run_gh(args: list[str]) -> Cmd:
    # Fixed gh argv. The token comes from GH_TOKEN, not from these arguments.
    proc = subprocess.run(  # noqa: S603
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    return Cmd(proc.returncode, proc.stdout, proc.stderr)


def _word_patterns(words: list[str]) -> tuple[re.Pattern[str], ...]:
    seen: list[re.Pattern[str]] = []
    for word in words:
        pattern = _word_pattern(word.strip().lower())
        if pattern.pattern not in {item.pattern for item in seen}:
            seen.append(pattern)
    return tuple(seen)


def load_reminder_config(path: Path, owner: str = "") -> ReminderConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    reminders = data.get("reminders") or {}
    claims = data.get("claims") or {}
    human = data.get("human_paths") or {}
    paths: list[re.Pattern[str]] = []
    for category in ("pricing", "security"):
        for pattern in human.get(category) or []:
            paths.append(re.compile(str(pattern), re.I))
    words = [str(item) for item in reminders.get("urgent_words") or []]
    assignee = (owner or str(reminders.get("assignee") or ASSIGNEE)).strip()
    # Never empty: an empty title fails issue creation, and an empty marker matches
    # every comment, so RIVET would edit someone else's comment as its own.
    return ReminderConfig(
        assignee=assignee,
        issue_title=str(reminders.get("issue_title") or "").strip() or f"RIVET: needs {assignee or 'owner'}",
        sticky_marker=str(reminders.get("sticky_marker") or "").strip() or "<!-- rivet-needs-owner -->",
        waiting_label=str(reminders.get("waiting_label") or "rivet:waiting"),
        remind_after_hours=int(reminders.get("remind_after_hours") or 24),
        urgent_remind_after_hours=int(reminders.get("urgent_remind_after_hours") or 4),
        urgent_labels=tuple(str(item) for item in reminders.get("urgent_labels") or []),
        urgent_words=_word_patterns(words),
        urgent_paths=tuple(paths),
        rerun_check_names=tuple(str(item) for item in reminders.get("rerun_check_names") or []),
        check_name=str(reminders.get("check_name") or "Rivet"),
        claim_label=str(claims.get("label") or "rivet:claimed"),
        claim_ttl_s=int(claims.get("claim_ttl_s") or 3600),
        foreign_claim_labels=tuple(str(item) for item in claims.get("foreign_labels") or []),
        rerun_cancelled=bool(reminders.get("rerun_cancelled", True)),
    )


def _event_payload() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH") or ""
    if not path or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _focus(event: str) -> int | None:
    raw = (os.environ.get("PR_NUMBER") or "").strip()
    if not raw:
        payload = _event_payload()
        number = (payload.get("pull_request") or {}).get("number") or (
            payload.get("issue") or {}
        ).get("number")
        raw = str(number or "")
    focus = int(raw) if raw.isdigit() else None
    if pull_read_plan(event, focus) == "list":
        return None
    return focus


def _write_json(payload: dict) -> str:
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w",
        encoding="utf-8",
        suffix=".json",
        delete=False,
    )
    with handle:
        json.dump(payload, handle)
    return handle.name


def _gh_json(client: GhClient, args: list[str]) -> object:
    result = client(args)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "gh failed").strip())
    if not (result.stdout or "").strip():
        return None
    return json.loads(result.stdout)


def _comments_from(raw: object) -> tuple[CommentView, ...]:
    if not isinstance(raw, list):
        return ()
    found: list[CommentView] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        created = str(item.get("createdAt") or item.get("created_at") or "")
        if not created:
            continue
        author = item.get("author") or item.get("user") or {}
        login = author.get("login") if isinstance(author, dict) else str(author or "")
        raw_id = item.get("id")
        comment_id = raw_id if isinstance(raw_id, int) else None
        if isinstance(raw_id, str) and raw_id.isdigit():
            comment_id = int(raw_id)
        found.append(
            CommentView(
                body=str(item.get("body") or ""),
                author=str(login or ""),
                created_at=_parse_time(created),
                comment_id=comment_id,
            )
        )
    return tuple(found)


def _checks_from(raw: object) -> tuple[CheckView, ...]:
    if not isinstance(raw, list):
        return ()
    found: list[CheckView] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("detailsUrl") or item.get("targetUrl") or "")
        found.append(
            CheckView(
                name=str(item.get("name") or item.get("context") or ""),
                conclusion=str(item.get("conclusion") or ""),
                url=url,
                run_id=run_id_from_url(url),
            )
        )
    return tuple(check for check in found if check.name)


def _snapshot_from_row(row: dict) -> PullSnapshot:
    author = row.get("author") or {}
    login = author.get("login") if isinstance(author, dict) else str(author or "")
    labels = []
    for item in row.get("labels") or []:
        if isinstance(item, str):
            labels.append(item)
        elif isinstance(item, dict) and item.get("name"):
            labels.append(str(item["name"]))
    files = []
    for item in row.get("files") or []:
        if isinstance(item, str):
            files.append(item)
        elif isinstance(item, dict) and item.get("path"):
            files.append(str(item["path"]))
    created = str(row.get("createdAt") or row.get("created_at") or "")
    return PullSnapshot(
        number=int(row["number"]),
        title=str(row.get("title") or ""),
        body=str(row.get("body") or ""),
        url=str(row.get("url") or ""),
        labels=tuple(labels),
        files=tuple(files),
        created_at=_parse_time(created) if created else datetime.now(UTC),
        head_sha=str(row.get("headRefOid") or row.get("head_sha") or ""),
        author=str(login or ""),
        checks=_checks_from(row.get("statusCheckRollup")),
        comments=_comments_from(row.get("comments")),
    )


def _post_or_update_comment(
    client: GhClient, repo: str, number: int, body: str, marker: str
) -> None:
    if not marker.strip():
        raise ValueError("refusing to post a sticky comment without a marker")
    listed = _gh_json(client, ["api", f"repos/{repo}/issues/{number}/comments?per_page=100"])
    comment_id = None
    if isinstance(listed, list):
        for item in listed:
            if isinstance(item, dict) and marker in str(item.get("body") or ""):
                comment_id = item.get("id")
                break
    payload = _write_json({"body": body})
    if comment_id:
        result = client(
            ["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}", "--input", payload]
        )
    else:
        result = client(
            ["api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "--input", payload]
        )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "comment failed").strip())


def _apply_decision(
    client: GhClient, repo: str, pull: PullSnapshot, decision: Decision, config: ReminderConfig
) -> None:
    if decision.sticky:
        _post_or_update_comment(client, repo, pull.number, decision.sticky, config.sticky_marker)
    if decision.fresh:
        result = client(
            ["pr", "comment", str(pull.number), "--repo", repo, "--body", decision.fresh]
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "remind failed").strip())
    for label in decision.add_labels:
        client(["pr", "edit", str(pull.number), "--repo", repo, "--add-label", label])
    for label in decision.remove_labels:
        client(["pr", "edit", str(pull.number), "--repo", repo, "--remove-label", label])
    for run_id in decision.rerun_run_ids:
        result = client(["run", "rerun", run_id, "--repo", repo, "--failed"])
        if result.returncode != 0:
            # A run whose only bad job was cancelled may have nothing GitHub calls
            # "failed"; re-run the whole run instead of leaving the check stuck.
            result = client(["run", "rerun", run_id, "--repo", repo])
        if result.returncode != 0:
            print(result.stderr or result.stdout)
    if decision.check and pull.head_sha:
        payload = _write_json(decision.check)
        result = client(["api", "-X", "POST", f"repos/{repo}/check-runs", "--input", payload])
        if result.returncode != 0:
            print(result.stderr or result.stdout)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and decision.check:
        text = str(decision.check["output"]["summary"])
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def _waiting_row(
    pull: PullSnapshot, reason: str, now: datetime, config: ReminderConfig
) -> WaitingRow:
    state = state_from_comments(pull.comments)
    started = (
        _parse_time(str(state["waiting_since"])) if state.get("waiting_since") else pull.created_at
    )
    waited = int((_aware(now) - _aware(started)).total_seconds())
    return WaitingRow(
        pull.number,
        pull.title,
        pull.url,
        max(0, waited),
        is_urgent(pull, config),
        reason,
    )


def _publish_issue(
    client: GhClient, repo: str, rows: list[WaitingRow], config: ReminderConfig, now: datetime
) -> None:
    found = _gh_json(
        client,
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            "100",
            "--json",
            "number,title,state",
            "--search",
            f"{config.issue_title} in:title",
        ],
    )
    existing = None
    if isinstance(found, list):
        for item in found:
            if isinstance(item, dict) and item.get("title") == config.issue_title:
                existing = item
                break
    body_path = _write_json({})
    # The issue body is markdown, not the JSON helper. Write it beside that file.
    text_path = body_path + ".md"
    Path(text_path).write_text(issue_body(sort_waiting(rows), now=now), encoding="utf-8")
    if existing is None:
        result = client(
            [
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                config.issue_title,
                "--assignee",
                config.assignee,
                "--body-file",
                text_path,
            ]
        )
    else:
        number = str(existing["number"])
        if str(existing.get("state") or "").lower() == "closed":
            client(["issue", "reopen", number, "--repo", repo])
        result = client(
            [
                "issue",
                "edit",
                number,
                "--repo",
                repo,
                "--add-assignee",
                config.assignee,
                "--body-file",
                text_path,
            ]
        )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "issue update failed").strip())


def _is_bot(actor: dict) -> bool:
    login = str(actor.get("login") or "")
    return str(actor.get("type") or "") == "Bot" or login.endswith("[bot]")


def auto_merge_turned_off_by(timeline: list) -> str:
    """Who turned auto-merge off, when that is still the latest word on it.

    A person turning auto-merge off on a pull request means "do not merge this yet". The
    lander merges green pull requests whether or not auto-merge is armed, so without this
    check that signal was ignored (a pull request once merged hours after a person turned
    auto-merge off, because it still carried rivet:waiting). Returns the login
    when the latest auto-merge event is a person's ``auto_merge_disabled``, else "". Turning
    auto-merge back on, or ``/rivet merge``, releases it. A bot's disable (GitHub drops
    auto-merge itself on a new push from a fork, a base change, or a conflict) is not a hold.
    """
    latest: dict | None = None
    for item in timeline or []:
        if isinstance(item, dict) and item.get("event") in (
            "auto_merge_enabled",
            "auto_merge_disabled",
        ):
            latest = item
    if not latest or latest.get("event") != "auto_merge_disabled":
        return ""
    actor = latest.get("actor") or {}
    if not isinstance(actor, dict) or _is_bot(actor):
        return ""
    return str(actor.get("login") or "")


def _auto_merge_holder(client: GhClient, repo: str, number: int) -> str:
    result = client(["api", "--paginate", f"repos/{repo}/issues/{number}/timeline?per_page=100"])
    if result.returncode != 0:
        # Fail closed: when we cannot tell, the lander does not merge it this run.
        return "unknown (timeline unreadable)"
    items: list = []
    # --paginate concatenates one JSON array per page.
    decoder = json.JSONDecoder()
    text = (result.stdout or "").strip()
    while text:
        page, end = decoder.raw_decode(text)
        if isinstance(page, list):
            items.extend(page)
        text = text[end:].lstrip()
    return auto_merge_turned_off_by(items)


def _cleanup_claims(
    client: GhClient, repo: str, numbers: list[int], config: ReminderConfig, now: datetime
) -> None:
    for number in numbers:
        try:
            timeline = _gh_json(client, ["api", f"repos/{repo}/issues/{number}/timeline"])
        except (RuntimeError, json.JSONDecodeError):
            continue
        events = []
        closed = False
        if isinstance(timeline, list):
            for item in timeline:
                if not isinstance(item, dict):
                    continue
                if item.get("event") == "closed":
                    closed = True
                label = item.get("label") or {}
                name = label.get("name") if isinstance(label, dict) else ""
                created = item.get("created_at") or ""
                if item.get("event") == "labeled" and name and created:
                    events.append(
                        {
                            "event": "labeled",
                            "label": str(name),
                            "created_at": _parse_time(str(created)),
                        }
                    )
        if claim_is_leaked(
            events, now=now, ttl_s=config.claim_ttl_s, closed=closed, label=config.claim_label
        ):
            client(
                [
                    "pr",
                    "edit",
                    str(number),
                    "--repo",
                    repo,
                    "--remove-label",
                    config.claim_label,
                ]
            )


def _runner_stdout(client: GhClient):
    from axiom.extensions.builtins.release.lander.adapters import ForgeUnavailable

    def runner(args: list[str]) -> str:
        result = client(args)
        if result.returncode != 0:
            raise ForgeUnavailable((result.stderr or result.stdout or "gh failed").strip())
        return result.stdout

    return runner


class _Idle:
    def __init__(self) -> None:
        self.outcomes: list = []

    def to_dict(self) -> dict:
        return {"stopped": "", "outcomes": []}


def _load_pulls(client: GhClient, repo: str, config_path: Path, focus: int | None):
    """One pull request on a pull-request event. The open set on a schedule."""
    from axiom.extensions.builtins.release.lander.adapters import GitHubForge, parse_pr
    from axiom.extensions.builtins.release.lander.config import load_config
    from axiom.infra.paths import get_user_state_dir

    state_dir = get_user_state_dir()
    lander = load_config(state_dir, repo, explicit=config_path)
    runner = _runner_stdout(client)
    if focus is None:
        forge = GitHubForge(
            runner=runner,
            limit=lander.pr_limit,
            self_landers=tuple(lander.self_landers),
        )
        loaded = forge.list_open(repo)
    else:
        raw = runner(["pr", "view", str(focus), "--repo", repo, "--json", _VIEW_FIELDS])
        row = json.loads(raw or "{}")
        loaded = [parse_pr(row, self_landers=tuple(lander.self_landers))]
    return loaded, lander, state_dir


def _act(client: GhClient, repo: str, loaded: list, lander, state_dir: Path):
    from axiom.extensions.builtins.release.lander.actor import GitEffects, run_actor
    from axiom.infra.orchestrator.approval import ApprovalGate
    from axiom.infra.orchestrator.approval_store import FileActionStore

    if not loaded:
        return _Idle()

    class _Backoff(GitEffects):
        def _run(self, args: list[str]) -> str:
            from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused

            last: Exception | None = None
            for attempt in range(5):
                try:
                    return GitEffects._run(self, args)
                except LanderRefused as exc:
                    last = exc
                    if not rate_limited(Cmd(1, "", str(exc))) or attempt == 4:
                        raise
                    time.sleep(retry_seconds(str(exc), attempt))
            raise last or RuntimeError("gh failed")

    gate = ApprovalGate(
        FileActionStore(
            state_dir / "agents" / "rivet" / "approvals.json",
            ttl_hours=lander.approval_ttl_hours,
        )
    )
    return run_actor(
        loaded,
        repo=repo,
        config=lander,
        state_dir=state_dir,
        effects=_Backoff(repo=repo, token_env=lander.token_env),
        gate=gate,
    )


def required_cancelled(client: GhClient, repo: str, number: int) -> frozenset[str]:
    """Names of required checks whose latest conclusion is cancelled.

    ``gh pr checks --required`` is the forge's own answer to which checks are
    required. It exits non-zero while checks are pending or none are required, and
    still prints the JSON, so the exit code is not read. Any error is no answer.
    """
    result = client(
        ["pr", "checks", str(number), "--repo", repo, "--required", "--json", "name,bucket"]
    )
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return frozenset()
    if not isinstance(rows, list):
        return frozenset()
    return frozenset(
        str(row.get("name"))
        for row in rows
        if isinstance(row, dict) and str(row.get("bucket") or "").lower() == "cancel"
    )


def _read_one(client: GhClient, repo: str, number: int) -> PullSnapshot:
    row = _gh_json(client, ["pr", "view", str(number), "--repo", repo, "--json", _VIEW_FIELDS])
    if not isinstance(row, dict):
        raise RuntimeError(f"could not read pull request {number}")
    return _snapshot_from_row(row)


def _ensure_label(client: GhClient, repo: str, name: str, color: str, description: str) -> None:
    listed = _gh_json(client, ["label", "list", "--repo", repo, "--limit", "200", "--json", "name"])
    names = set()
    if isinstance(listed, list):
        names = {str(item.get("name")) for item in listed if isinstance(item, dict)}
    if name not in names:
        client(
            [
                "label",
                "create",
                name,
                "--repo",
                repo,
                "--color",
                color,
                "--description",
                description,
            ]
        )


def run_command(client: GhClient, config: ReminderConfig) -> int:
    payload = _event_payload()
    author = ""
    body = ""
    number = _focus(os.environ.get("GITHUB_EVENT_NAME") or "")
    if "comment" in payload:
        author = str((payload["comment"].get("user") or {}).get("login") or "")
        body = str(payload["comment"].get("body") or "")
        number = int((payload.get("issue") or {}).get("number") or number or 0) or number
    elif "review" in payload:
        author = str((payload["review"].get("user") or {}).get("login") or "")
        body = str(payload["review"].get("body") or "")
        number = int((payload.get("pull_request") or {}).get("number") or number or 0) or number
    parsed = parse_command(body)
    if not parsed or not author_allowed(author) or not number:
        print("ignored rivet command")
        return 0
    repo = (os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    snap = _read_one(client, repo, number)
    state = state_from_comments(snap.comments)
    run_ids = tuple(
        check.run_id
        for check in snap.checks
        if check.run_id and check.conclusion.upper() == "FAILURE"
    )
    plan = command_plan(
        parsed,
        author=author,
        number=number,
        repo=repo,
        state=state,
        run_ids=run_ids if parsed[0] in {"rerun", "option"} else (),
    )
    for args in plan:
        result = client(args)
        if result.returncode != 0 and args[:2] == ["pr", "merge"]:
            admin = [*args, "--admin"]
            result = client(admin)
        if result.returncode != 0:
            print(result.stderr or result.stdout)
            return 1
    print(f"acted on /rivet {parsed[0]} for {repo}#{number}")
    return 0


def run_land(
    client: GhClient, config: ReminderConfig, config_path: Path, *, act: bool = True
) -> int:
    """One landing pass. ``act`` False reports only: nothing may be merged."""
    event = os.environ.get("EVENT_NAME") or os.environ.get("GITHUB_EVENT_NAME") or "schedule"
    focus = _focus(event)
    if pull_read_plan(event, focus) == "view" and focus is None:
        print("a pull request event needs a pull request number", file=sys.stderr)
        return 1
    repo = (os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if not repo:
        print("REPO is required", file=sys.stderr)
        return 1
    now = datetime.now(UTC)
    _ensure_label(client, repo, config.waiting_label, "fbca04", "Waiting on the owner via RIVET")
    _ensure_label(client, repo, config.claim_label, "5319e7", "Claimed by the Rivet PR lander")
    loaded, lander, state_dir = _load_pulls(client, repo, config_path, focus)
    claim_numbers: list[int] = []
    if focus is None:
        listed = _gh_json(
            client,
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--label",
                config.claim_label,
                "--state",
                "all",
                "--limit",
                "100",
                "--json",
                "number",
            ],
        )
        if isinstance(listed, list):
            claim_numbers = [int(item["number"]) for item in listed if isinstance(item, dict)]
    else:
        claim_numbers = [pull.number for pull in loaded if config.claim_label in pull.labels]
    _cleanup_claims(client, repo, claim_numbers, config, now)

    held_back: set[int] = set()
    reran: list[WaitingRow] = []
    for pull in loaded:
        names = [item.name for item in pull.failing_checks]
        stale = any(
            pattern.lower() in name.lower()
            for pattern in config.rerun_check_names
            for name in names
        )
        cancelled: frozenset[str] = frozenset()
        if names and config.rerun_cancelled:
            cancelled = required_cancelled(client, repo, pull.number)
        if not stale and not cancelled:
            continue
        snap = _read_one(client, repo, pull.number)
        preview = decide_pull(
            pull=snap,
            outcome=Outcome("escalated", "checks not green"),
            now=now,
            config=config,
            required_cancelled=cancelled,
        )
        if preview.rerun_run_ids:
            _apply_decision(client, repo, snap, preview, config)
            held_back.add(pull.number)
            reran.append(
                _waiting_row(snap, "reran stale or cancelled checks before escalating", now, config)
            )
    for pull in loaded:
        if pull.number in held_back:
            continue
        holder = _auto_merge_holder(client, repo, pull.number)
        if not holder:
            continue
        held_back.add(pull.number)
        print(f"#{pull.number}: auto-merge turned off by {holder}; not landing it")
        try:
            snap = _read_one(client, repo, pull.number)
        except (RuntimeError, json.JSONDecodeError, KeyError, ValueError):
            continue
        reran.append(
            _waiting_row(
                snap,
                f"auto-merge was turned off by {holder}; turn it back on or reply /rivet merge to land",
                now,
                config,
            )
        )
    acted = _act(
        client,
        repo,
        [pull for pull in loaded if pull.number not in held_back],
        lander if act else dataclasses.replace(lander, auto_allowed=()),
        state_dir,
    )
    output = os.environ.get("LANDER_OUTPUT_FILE") or ""
    if output:
        Path(output).write_text(json.dumps(acted.to_dict(), indent=2), encoding="utf-8")
    rows: list[WaitingRow] = []
    for outcome in acted.outcomes:
        if outcome.number in held_back:
            continue
        try:
            snap = _read_one(client, repo, outcome.number)
        except (RuntimeError, json.JSONDecodeError, KeyError, ValueError) as exc:
            print(f"could not refresh #{outcome.number}: {exc}")
            continue
        decision = decide_pull(
            pull=snap,
            outcome=Outcome(outcome.status, outcome.reason),
            now=now,
            config=config,
        )
        if decision.rerun_run_ids:
            _apply_decision(client, repo, snap, decision, config)
            continue
        if decision.escalate and decision.sticky:
            _apply_decision(client, repo, snap, decision, config)
            rows.append(_waiting_row(snap, outcome.reason, now, config))
        elif decision.remove_labels:
            for label in decision.remove_labels:
                client(["pr", "edit", str(snap.number), "--repo", repo, "--remove-label", label])
    if focus is None:
        _publish_issue(client, repo, [*reran, *rows], config, now)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    config_path = Path(".axiom/release-lander.toml")
    if "--config" in argv:
        config_path = Path(argv[argv.index("--config") + 1])
    if not os.environ.get("GH_TOKEN"):
        print("GH_TOKEN is required", file=sys.stderr)
        return 1 if os.environ.get("GITHUB_ACTIONS") else 0
    config = load_reminder_config(config_path, os.environ.get("OWNER", ""))
    configure(owner=config.assignee, issue_title=config.issue_title, marker=config.sticky_marker)
    act = "--act" in argv
    client = GhClient(_run_gh, time.sleep)
    event = os.environ.get("EVENT_NAME") or os.environ.get("GITHUB_EVENT_NAME") or ""
    try:
        if event in {"issue_comment", "pull_request_review"}:
            return run_command(client, config)
        return run_land(client, config, config_path, act=act)
    except (RuntimeError, json.JSONDecodeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
