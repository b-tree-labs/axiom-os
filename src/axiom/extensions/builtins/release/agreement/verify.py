# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""AGREED, NOT_AGREED or UNCERTAIN: did the owner agree in chat to this pull request's work?

The evidence is two files the harness wrote: the prompt log (the hook) and the
session transcript (Claude Code). A record only points at them. Nothing in a pull
request, a tool result, a message from another session or an agent's own account
can create an agreement, and the subject below has no field for such text.

NOT_AGREED means the evidence says no (no record, a forged one, not the owner's
words, a no, a hold since, expired). UNCERTAIN means it cannot be told (the reply
was not a clear yes to named work, the scope grew, an irreversible action was not
named, the transcript is not here). Both wait for the owner's GitHub review.
Only AGREED can skip it, and in shadow mode even that changes nothing.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import affirm, paths, store
from . import transcript as tr

AGREED = "AGREED"
NOT_AGREED = "NOT_AGREED"
UNCERTAIN = "UNCERTAIN"

#: A yes expires after this long, and silence never extends it (issue #2471).
DEFAULT_WINDOW = timedelta(hours=24)
#: A prompt this fresh may not be in the transcript yet (the file is written asynchronously).
TRANSCRIPT_LAG = timedelta(seconds=120)

_BLANKET = frozenset({"", "*", "**", "**/*", "/", ".", "./", "*/", "**/"})
_NOT_OWNER = {
    tr.TOOL_RESULT: "tool_result",
    tr.PEER: "peer",
    tr.TASK_NOTIFICATION: "task_notification",
    tr.SUMMARY: "summary",
    tr.COMMAND: "command",
    tr.SYSTEM: "system",
    tr.ASSISTANT: "assistant",
    tr.OTHER: "other",
}


@dataclass(frozen=True)
class Subject:
    """A pull request as the lander sees it. No body, no comments: text there is data, never evidence."""

    repo: str
    number: int
    head_sha: str
    head_ref: str = ""
    title: str = ""
    labels: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    #: When the pull request's first commit was made, or None when unknown (a carry then cannot be shown).
    first_commit_at: datetime | None = None


@dataclass(frozen=True)
class Verdict:
    decision: str
    reasons: tuple[str, ...]
    record_id: str = ""
    uncovered: tuple[str, ...] = ()
    detail: str = ""
    session_id: str = ""
    agreed_at: str = ""
    words: str = ""
    named_actions: tuple[str, ...] = ()
    extras: dict = field(default_factory=dict, compare=False)

    @property
    def skips_review(self) -> str:
        return {AGREED: "yes", NOT_AGREED: "no"}.get(self.decision, "uncertain")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def covered(path: str, scope: tuple[str, ...]) -> bool:
    for entry in scope:
        entry = entry.strip()
        if not entry or entry in _BLANKET:
            continue
        if any(ch in entry for ch in "*?["):
            if fnmatch.fnmatchcase(path, entry):
                return True
        elif entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry or path.startswith(entry + "/"):
            return True
    return False


def _is_blanket(scope: tuple[str, ...]) -> bool:
    return not scope or any(entry.strip() in _BLANKET for entry in scope)


# --- irreversible actions ---------------------------------------------------

_TEXT_RULES = {
    "release": re.compile(r"\brelease[sd]?\b|\bpublish(es|ed|ing)?\b", re.I),
    "tag": re.compile(r"\btag(s|ged|ging)?\b|\bretag", re.I),
    "node_change": re.compile(
        r"\bdeploy(s|ed|ing|ment)?\b|\brollout\b|\broll(ed)? back\b|\brollback\b", re.I
    ),
    "secrets": re.compile(
        r"\bsecrets?\b|\bcredentials?\b|\bvault\b|\brotat(e|es|ed|ion)\b|\bapi[- ]keys?\b", re.I
    ),
    "delete_data": re.compile(
        r"\b(delete|drop|purge|wipe|truncate|destroy)\b.{0,40}\b(data|tables?|database|records?|rows?|"
        r"backups?|volumes?|buckets?|snapshots?)\b",
        re.I,
    ),
}
_NODE_DIRS = ("deploy/", "charts/", "k8s/", "helm/", "nodes/", "infra/")
_SECRET_PARTS = {"vault", "secrets", "credentials"}
_DESTRUCTIVE_NAME = re.compile(r"drop|delete|purge|truncate|wipe", re.I)


def detect_actions(subject: Subject) -> tuple[str, ...]:
    """Irreversible actions the pull request itself shows. Text here can only raise the bar, never lower it."""
    found: set[str] = set()
    # "fix(release): ..." names the area a change is in, not an act of releasing.
    title = re.sub(r"^\s*\w+(\([^)]*\))?!?:\s*", "", subject.title)
    branch = subject.head_ref if subject.head_ref.startswith(("release/", "hotfix/")) else ""
    text = " ".join((title, branch, *subject.labels))
    for action, rule in _TEXT_RULES.items():
        if rule.search(text):
            found.add(action)
    for path in subject.files:
        lowered = path.lower()
        parts = lowered.split("/")
        if lowered.startswith(_NODE_DIRS) or "/deploy/" in lowered:
            found.add("node_change")
        if _SECRET_PARTS & set(parts) or parts[-1].startswith(".env"):
            found.add("secrets")
        if lowered.startswith(".github/workflows/release"):
            found.add("release")
        if "migrations/" in lowered and _DESTRUCTIVE_NAME.search(parts[-1]):
            found.add("delete_data")
    return tuple(sorted(found))


# --- the check ---------------------------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _find_turn(turns: list[tr.Turn], entry: store.PromptEntry) -> int | None:
    wanted = _norm(entry.prompt)
    best: tuple[tuple[int, float], int] | None = None
    for position, turn in enumerate(turns):
        if turn.kind == tr.ASSISTANT:
            continue
        if turn.session_id and entry.session_id and turn.session_id != entry.session_id:
            continue
        by_id = bool(entry.prompt_id) and turn.prompt_id == entry.prompt_id
        texts = {_norm(turn.text), _norm(turn.searchable)}
        same = wanted in texts or (by_id and any(t and (wanted in t or t in wanted) for t in texts))
        if not same:
            continue
        gap = abs((turn.time - entry.time).total_seconds()) if turn.time else 1e9
        rank = (0 if by_id else 1, gap)
        if best is None or rank < best[0]:
            best = (rank, position)
    return None if best is None else best[1]


def _mentions_pull(turns: list[tr.Turn], subject: Subject) -> bool:
    number = subject.number
    rules = [
        re.compile(rf"/pull/{number}\b"),
        re.compile(rf"\b(?:PR|pull request|pull)\s*#?{number}\b", re.I),
    ]
    for turn in turns:
        text = turn.searchable
        if any(rule.search(text) for rule in rules):
            return True
        if subject.head_ref and subject.head_ref in text:
            return True
    return False


def _scope_heard(record: store.Record, proposal: str) -> bool:
    scope = [p for p in record.paths if p.strip() not in _BLANKET]
    return bool(scope) and all(p.strip() in proposal for p in scope)


def _carry_work_matches(record: store.Record, subject: Subject, proposal: str) -> bool:
    if subject.head_ref and subject.head_ref.lower() in proposal.lower():
        return True
    tickets = re.findall(r"#\d{2,}", record.work)
    cited = f"{subject.title} {subject.head_ref}"
    if tickets and all(t in cited for t in tickets):
        return True
    return _scope_heard(record, proposal)


def _pointer_cites(record: store.Record, projects_root: Path, yes_time: datetime) -> str:
    """Empty when the handoff pointer is a real message or note that cites the record; otherwise the reason."""
    handoff = record.handoff or {}
    kind, ref = str(handoff.get("kind", "")), str(handoff.get("ref", ""))
    target = Path(ref) if ref else None
    if target is None or not _inside(target, projects_root) or not target.is_file():
        return "handoff_pointer_not_found"
    if kind == "cross-session-message":
        try:
            turns = tr.read_transcript(target)
        except OSError:
            return "handoff_pointer_not_found"
        for turn in turns:
            if (
                turn.kind == tr.PEER
                and record.id in turn.text
                and (turn.time is None or turn.time >= yes_time)
            ):
                return ""
        return "handoff_pointer_does_not_cite_the_record"
    if kind == "memory":
        try:
            stamp = datetime.fromtimestamp(target.stat().st_mtime, UTC)
            if (
                record.id in target.read_text(encoding="utf-8", errors="replace")
                and stamp >= yes_time
            ):
                return ""
        except OSError:
            return "handoff_pointer_not_found"
        return "handoff_pointer_does_not_cite_the_record"
    return "handoff_pointer_kind_unknown"


def _decide(reasons: list[str], hard_no: set[str]) -> str:
    if any(r.split(":")[0] in hard_no or r in hard_no for r in reasons):
        return NOT_AGREED
    return UNCERTAIN if reasons else AGREED


_NO_REASONS = {
    "no_record",
    "message_not_logged",
    "message_hash_mismatch",
    "message_not_in_transcript",
    "owner_said_no",
    "voided_by_later_message",
    "expired",
    "record_predates_message",
    "not_owner_prompt",
}


def _check(
    record: store.Record,
    subject: Subject,
    *,
    log: store.PromptLog,
    projects_root: Path,
    now: datetime,
    window: timedelta,
    extra_prompts: tuple[store.PromptEntry, ...],
) -> Verdict:
    entry = log.find(record.message_id)
    if entry is None:
        return Verdict(
            NOT_AGREED,
            ("message_not_logged",),
            record.id,
            detail="the cited message is not in the prompt log",
        )
    if not log.intact:
        return Verdict(
            UNCERTAIN,
            ("prompt_log_chain_broken",),
            record.id,
            detail="the prompt log was edited after it was written",
        )
    if store.sha(entry.prompt) != record.message_sha:
        return Verdict(
            NOT_AGREED,
            ("message_hash_mismatch",),
            record.id,
            detail="the record does not match the logged message",
        )
    if entry.truncated:
        return Verdict(
            UNCERTAIN, ("message_truncated",), record.id, detail="the logged message was cut short"
        )

    transcript_path = Path(entry.transcript_path) if entry.transcript_path else None
    if transcript_path is None or not _inside(transcript_path, projects_root):
        return Verdict(
            UNCERTAIN,
            ("transcript_not_local",),
            record.id,
            detail="the session is not one of this machine's local transcripts",
        )
    if not transcript_path.is_file():
        return Verdict(
            UNCERTAIN,
            ("transcript_not_found",),
            record.id,
            detail="the session transcript cannot be found",
        )
    try:
        turns = tr.read_transcript(transcript_path)
    except OSError:
        return Verdict(
            UNCERTAIN,
            ("transcript_not_found",),
            record.id,
            detail="the session transcript cannot be read",
        )

    position = _find_turn(turns, entry)
    if position is None:
        if now - entry.time < TRANSCRIPT_LAG:
            return Verdict(
                UNCERTAIN,
                ("transcript_may_lag",),
                record.id,
                detail="the message is too new to be in the transcript yet",
            )
        return Verdict(
            NOT_AGREED,
            ("message_not_in_transcript",),
            record.id,
            detail="no such message in the session transcript",
        )
    turn = turns[position]
    if turn.kind != tr.OWNER:
        name = _NOT_OWNER.get(turn.kind, "other")
        return Verdict(
            NOT_AGREED,
            (f"not_owner_prompt:{name}",),
            record.id,
            detail=f"the cited message is a {name.replace('_', ' ')}, not something the owner typed",
        )

    yes_time = turn.time or entry.time
    reasons: list[str] = []
    detail: list[str] = []

    created = tr.parse_time(record.created_at)
    if created is None or created < yes_time:
        reasons.append("record_predates_message")
    if now - yes_time > window:
        reasons.append("expired")
        detail.append(f"the yes is older than {int(window.total_seconds() // 3600)} hours")

    reading = affirm.reading(entry.prompt)
    if reading == affirm.HOLD:
        reasons.append("owner_said_no")
    elif reading != affirm.YES:
        reasons.append("not_affirmative")
        detail.append("the reply is not a clear yes")

    proposal = ""
    for earlier in reversed(turns[:position]):
        if earlier.kind == tr.ASSISTANT and earlier.text.strip():
            proposal = earlier.text
            break
        if earlier.kind in (tr.OWNER, tr.COMMAND):
            break
    if not proposal:
        reasons.append("no_assistant_proposal")
        detail.append("no assistant message just before the yes")
    elif not affirm.names_work(
        proposal, work=record.work, pr=subject.number, branch=subject.head_ref
    ):
        reasons.append("assistant_did_not_name_the_work")
        detail.append("the message the owner answered did not name the work")

    owner_words = affirm.owner_words(entry.prompt)
    uncovered: tuple[str, ...] = ()
    if _is_blanket(record.paths):
        reasons.append("scope_is_blanket")
        detail.append("the record names no specific scope")
    elif not subject.files:
        reasons.append("files_unknown")
    else:
        uncovered = tuple(sorted(f for f in subject.files if not covered(f, record.paths)))
        if uncovered:
            reasons.append("paths_outside_scope")
            detail.append("paths outside what was agreed: " + ", ".join(uncovered))

    needed = tuple(sorted(set(detect_actions(subject)) | set(record.actions)))
    for action in needed:
        named = affirm.names_action(proposal, action) or affirm.names_action(owner_words, action)
        if action not in record.actions or not named:
            reasons.append(f"irreversible_action_not_named:{action}")
            detail.append(
                f"the owner was not asked about the irreversible step: {action.replace('_', ' ')}"
            )

    work_terms = tuple(affirm.content_words(record.work))
    for later in (*log.entries, *extra_prompts):
        if later.id == entry.id or later.time <= yes_time or later.time > now:
            continue
        if affirm.is_hold(
            later.prompt, pr=subject.number, branch=subject.head_ref, work_terms=work_terms
        ):
            reasons.append("voided_by_later_message")
            detail.append(
                f'a later message in session {later.session_id[:8]} says "{_norm(affirm.owner_words(later.prompt))[:60]}"'
            )
            break

    carried = not _mentions_pull(turns, subject)
    if carried:
        reasons_before = len(reasons)
        if not record.handoff:
            reasons.append("carry_needs_handoff_pointer")
        else:
            problem = _pointer_cites(record, projects_root, yes_time)
            if problem:
                reasons.append(problem)
        first = subject.first_commit_at
        if first is None:
            reasons.append("first_commit_time_unknown")
        elif first < yes_time:
            reasons.append("pull_request_predates_the_yes")
        elif first > yes_time + window:
            reasons.append("pull_request_started_after_the_window")
        if proposal and not _carry_work_matches(record, subject, proposal):
            reasons.append("carry_work_not_matched")
        if len(reasons) > reasons_before:
            detail.append("another session opened this pull request and the carry is not shown")
        else:
            reasons.append("carried_from_session")

    soft = [r for r in reasons if r != "carried_from_session"]
    decision = _decide(soft, _NO_REASONS)
    if decision == AGREED:
        sentence = (
            f'agreed in session {entry.session_id} at {yes_time.isoformat()}: "{owner_words}"'
        )
    else:
        sentence = "; ".join(detail) or ", ".join(soft)
    return Verdict(
        decision,
        tuple(reasons),
        record.id,
        uncovered=uncovered,
        detail=sentence,
        session_id=entry.session_id,
        agreed_at=yes_time.isoformat(),
        words=owner_words,
        named_actions=needed,
    )


_ORDER = {AGREED: 0, UNCERTAIN: 1, NOT_AGREED: 2}


def verify(
    subject: Subject,
    *,
    state_dir: Path | str | None = None,
    projects_root: Path | str | None = None,
    now: datetime | None = None,
    window: timedelta = DEFAULT_WINDOW,
    extra_prompts: tuple[store.PromptEntry, ...] = (),
) -> Verdict:
    """The verdict for one pull request. The best record wins, and nothing else is read."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    root = Path(projects_root) if projects_root else paths.default_projects_root()
    records = [
        r
        for r in store.read_records(state_dir)
        if r.repo == subject.repo and r.pr == subject.number
    ]
    if not records:
        return Verdict(
            NOT_AGREED, ("no_record",), detail="no agreement record for this pull request"
        )
    log = store.read_prompts(state_dir)
    verdicts = [
        _check(
            r,
            subject,
            log=log,
            projects_root=root,
            now=moment,
            window=window,
            extra_prompts=extra_prompts,
        )
        for r in sorted(records, key=lambda r: r.created_at, reverse=True)
    ]
    return min(verdicts, key=lambda v: _ORDER[v.decision])
