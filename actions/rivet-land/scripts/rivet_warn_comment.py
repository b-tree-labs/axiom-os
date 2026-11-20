#!/usr/bin/env python3
"""Sticky comment for the Rivet pull-request lander.

People see plain English: a lead sentence, the blocking reasons, links to
failing checks, and the next step. The lander JSON is not inlined. It is
uploaded as a workflow artifact and, on the pull request comment only,
collapsed at the bottom.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from rivet_hold_check import Comment, parse_time, resolve_hold

MARKER = "<!-- rivet-lander-warn -->"
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_BAD_CHECKS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}


def redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def short_date(value: date) -> str:
    return f"{_MONTHS[value.month - 1]} {value.day}"


def extract_json(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def outcome_for(payload: dict | None, number: int | None) -> dict | None:
    if not payload or number is None:
        return None
    for item in payload.get("outcomes") or []:
        if isinstance(item, dict) and item.get("number") == number:
            return item
    return None


def _checks_clause(failing: list[dict]) -> str:
    count = len(failing)
    word = "check" if count == 1 else "checks"
    names = ", ".join(str(item.get("name") or "check") for item in failing)
    return f"{count} {word} failing ({names})"


def _held_clause(hold: dict) -> str:
    reason = str(hold.get("reason") or "no Hold: comment yet")
    text = f"Held: do-not-merge, reason: {reason}"
    review = str(hold.get("review_by_text") or "").strip()
    if review:
        text += f", review by {review}"
    return text


def _other_blocks(outcome: dict | None) -> list[str]:
    if not outcome:
        return []
    reason = str(outcome.get("reason") or "")
    status = str(outcome.get("status") or "")
    if status == "landed" or "do-not-merge" in reason:
        return []
    if reason == "draft":
        return ["this pull request is a draft"]
    if reason == "conflict":
        return ["the branch conflicts with main"]
    if reason.startswith("waiting on "):
        return [reason]
    if reason.startswith("protected"):
        kind = reason.split(":", 1)[-1] or "path"
        return [f"protected path ({kind})"]
    if reason.startswith("changes requested"):
        return [reason]
    if reason in {"checks not green", "no fixer configured"}:
        return []
    if reason and status in {"escalated", "waiting", "skipped"}:
        return [reason.replace("_", " ")]
    return []


def human_lines(
    *,
    outcome: dict | None,
    failing: list[dict] | None = None,
    hold: dict | None = None,
    labels: list[str] | None = None,
    running: int = 0,
) -> tuple[str, list[str], str]:
    """Lead sentence, blocking bullets, and the next step."""
    failing = failing or []
    labels = labels or []
    status = str((outcome or {}).get("status") or "")
    reason = str((outcome or {}).get("reason") or "")
    held = hold is not None or "do-not-merge" in labels or "do-not-merge" in reason
    if status == "landed":
        return (
            "Landed: this pull request was merged.",
            [],
            "Next step: none.",
        )
    sentences: list[str] = []
    clause_bits: list[str] = []
    if failing:
        clause_bits.append(_checks_clause(failing))
    if running and not failing:
        clause_bits.append(f"{running} checks still running")
    clause_bits.extend(_other_blocks(outcome))
    if clause_bits:
        sentences.append("Not landing yet: " + ". ".join(clause_bits) + ".")
    if held:
        sentences.append(_held_clause(hold or {"reason": "labeled do-not-merge"}) + ".")
    if not sentences:
        sentences.append("Ready to land: all required checks passed.")
    bullets: list[str] = []
    for check in failing:
        name = str(check.get("name") or "check")
        url = str(check.get("url") or "")
        if url:
            bullets.append(f"- [{name}]({url}) is failing.")
        else:
            bullets.append(f"- {name} is failing.")
    for block in _other_blocks(outcome):
        bullets.append(f"- {block[0].upper()}{block[1:]}.")
    releases = str((hold or {}).get("releases") or "").strip()
    if held:
        who = releases or "someone"
        bullets.append(f"- The do-not-merge label stays until {who} removes it.")
    steps: list[str] = []
    if failing:
        steps.append("fix the failing checks and push again")
    if held:
        who = releases or "someone"
        steps.append(f"the hold stays until {who} removes the do-not-merge label")
    elif any(bit.startswith("waiting on ") for bit in clause_bits):
        waiting = next(bit for bit in clause_bits if bit.startswith("waiting on "))
        steps.append(waiting)
    elif any(bit.startswith("this pull request is a draft") for bit in clause_bits):
        steps.append("mark this pull request ready for review")
    elif any(bit.startswith("protected path") for bit in clause_bits):
        steps.append("a person merges this pull request. The lander will not")
    if sentences[0].startswith("Ready to land"):
        nxt = "Next step: none. A later lander run can merge this pull request."
    elif not steps:
        nxt = "Next step: wait for the checks to finish, then look again."
    else:
        bits = [step[0].upper() + step[1:] for step in steps]
        nxt = "Next step: " + ". ".join(bits) + "."
    return " ".join(sentences), bullets, nxt


def fleet_lines(payload: dict | None) -> tuple[str, list[str], str]:
    outcomes = (payload or {}).get("outcomes") or []
    landed = [
        item for item in outcomes if isinstance(item, dict) and item.get("status") == "landed"
    ]
    if landed:
        numbers = ", ".join(f"#{item.get('number')}" for item in landed)
        lead = f"Landed: {numbers}."
    else:
        lead = "No pull request was landed on this run."
    # Every non-draft pull request gets its own line: those are the ones a person
    # can act on. Drafts fold into one line. A fixed row cap once let drafts push
    # a green, held pull request off the list.
    bullets = []
    drafts: list[str] = []
    for item in outcomes:
        if not isinstance(item, dict) or item.get("status") == "landed":
            continue
        why = str(item.get("reason") or item.get("status") or "held").replace("_", " ")
        if why == "draft":
            drafts.append(f"#{item.get('number')}")
            continue
        bullets.append(f"- #{item.get('number')}: {why}.")
    if drafts:
        bullets.append(f"- Drafts ({len(drafts)}): {', '.join(drafts)}.")
    return (
        lead,
        bullets,
        "Next step: open one pull request to see its checks and its hold.",
    )


_RISK_AREAS = (
    ("pricing/billing", re.compile(r"pricing|billing|stripe|invoice", re.I)),
    ("security", re.compile(r"(^|/)(security|secrets?)(/|$)|credential", re.I)),
    (
        "data/schema/migrations",
        re.compile(r"migration|schema|\.sql$", re.I),
    ),
    (
        "dependencies",
        re.compile(
            r"(package(?:-lock)?\.json|yarn\.lock|pnpm-lock[^/]*|requirements[^/]*\.txt|"
            r"pyproject\.toml|poetry\.lock|uv\.lock|Dockerfile(?:\.[^/]+)?|go\.(?:mod|sum))$",
            re.I,
        ),
    ),
    (
        "CI config",
        re.compile(r"\.github/(?:workflows|scripts)/|\.axiom/release-lander\.toml$", re.I),
    ),
    (
        "auth/access roles",
        re.compile(
            r"(^|/)(?:auth|rbac|permissions?|roles?)(?:\.[^/]+)?(/|$)|_role",
            re.I,
        ),
    ),
)
_CHECK_THIS = {
    "pricing/billing": "Check prices and Stripe amounts against the approved figure.",
    "security": "Check secrets, auth, and who can call the changed endpoint.",
    "data/schema/migrations": "Read the migration for locks, backfill, and rollback.",
    "dependencies": "Confirm the bumped package and that the lockfile matches.",
    "CI config": "Confirm the workflow cannot land or deploy past the stated hold.",
    "auth/access roles": "Check role and permission changes stay least privilege.",
}
_CHECK_FILLERS = (
    "Skim the diff against the summary above.",
    "Run the tests named in the pull request body.",
)


def sticky_comment_jq() -> str:
    """Select the existing lander comment so a later run updates it."""
    return f'[.[] | select(.body | contains("{MARKER}"))][0].id'


def _clip(text: str, limit: int = 280) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _period(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    if stripped[-1] in ".!?":
        return stripped
    return stripped + "."


def _body_sentences(body: str) -> list[str]:
    text = re.sub(r"<!--.*?-->", " ", body or "", flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("|"):
            continue
        if stripped.startswith(("- [", "* [")):
            continue
        if stripped.lower() in {"summary", "test plan"}:
            continue
        kept.append(stripped)
    blob = " ".join(kept)
    parts = re.split(r"(?<=[.!?])\s+", blob)
    sentences = [_clip(part) for part in parts if part.strip()]
    return [part for part in sentences if part.lower().strip(".") not in {"draft", "n/a"}]


def _risk_hits(files: list[dict]) -> list[str]:
    paths = [str(item.get("path") or "") for item in files]
    found: list[str] = []
    for name, pattern in _RISK_AREAS:
        if any(pattern.search(path) for path in paths):
            found.append(name)
    return found


def _top_dirs(files: list[dict]) -> list[str]:
    tops: list[str] = []
    for item in files:
        path = str(item.get("path") or "")
        if not path:
            continue
        parts = path.split("/")
        label = "/".join(parts[:2]) if len(parts) > 1 else parts[0]
        if label not in tops:
            tops.append(label)
    return tops


def _linked(title: str, body: str) -> str:
    blob = f"{title}\n{body}"
    tickets = list(dict.fromkeys(re.findall(r"PENG-\d+", blob)))
    stacked = list(dict.fromkeys(re.findall(r"(?i)stack(?:ed)?(?:\s+on)?\s+#(\d+)", body or "")))
    bits: list[str] = []
    if tickets:
        bits.append(", ".join(tickets))
    if stacked:
        bits.append("stacked-on #" + ", #".join(stacked))
    return "Links: " + ("; ".join(bits) if bits else "none") + "."


def reviewer_section(
    *,
    title: str,
    body: str,
    files: list[dict] | None = None,
    hold: dict | None = None,
) -> str:
    """Short brief for a person reviewing the pull request. No JSON."""
    files = files or []
    sentences = _body_sentences(body)
    tops = _top_dirs(files)
    path_sentence = ""
    if files:
        shown = ", ".join(f"`{name}`" for name in tops[:3])
        extra = f" and {len(tops) - 3} more" if len(tops) > 3 else ""
        path_sentence = f"It edits {len(files)} files under {shown}{extra}."
    if not sentences:
        summary = _period(title or "This pull request")
        if path_sentence:
            summary = f"{summary} {path_sentence}"
    elif len(sentences) == 1 and path_sentence:
        summary = f"{_period(sentences[0])} {path_sentence}"
    else:
        summary = " ".join(_period(sentence) for sentence in sentences[:2])
    risks = _risk_hits(files)
    if risks:
        areas = "Areas: " + ", ".join(f"{name} (risk)" for name in risks) + "."
    else:
        areas = "Areas: no pricing, security, data, dependency, CI, or auth risk flags."
    additions = sum(int(item.get("additions") or 0) for item in files)
    deletions = sum(int(item.get("deletions") or 0) for item in files)
    size = f"Size: {len(files)} files, +{additions} −{deletions}."
    if hold:
        hold_line = f"Hold: do-not-merge. Reason: {hold.get('reason') or 'unspecified'}."
        review = str(hold.get("review_by_text") or "").strip()
        if review:
            hold_line += f" Review by {review}."
    else:
        hold_line = "Hold: none."
    pointers = [_CHECK_THIS[name] for name in risks]
    for filler in _CHECK_FILLERS:
        if len(pointers) >= 2:
            break
        pointers.append(filler)
    pointers = pointers[:4]
    lines = [
        "## For reviewers",
        "",
        summary,
        "",
        areas,
        size,
        _linked(title, body),
        hold_line,
        "",
        "Check this:",
    ]
    lines.extend(f"- {item}" for item in pointers)
    return "\n".join(lines)


def visible_body(
    *,
    output: str,
    version: str,
    pin_ready: bool,
    pr_number: int | None = None,
    failing: list[dict] | None = None,
    hold: dict | None = None,
    labels: list[str] | None = None,
    running: int = 0,
    title: str = "",
    pr_body: str = "",
    files: list[dict] | None = None,
) -> str:
    """Plain-English status. No lander JSON."""
    version_line = version.strip() or "(unset)"
    if pin_ready:
        banner = f"Ran `axiom-os-lm=={version_line}`."
    else:
        banner = (
            "TODO: pin `axiom-os-lm` to the first release newer than 0.70.3. "
            "0.70.3 does not ship `python -m axiom.extensions.builtins.release land`. "
            f"Workflow pin is `{version_line}`."
        )
    payload = extract_json(output)
    if pr_number is None and payload and payload.get("outcomes"):
        lead, bullets, nxt = fleet_lines(payload)
    else:
        lead, bullets, nxt = human_lines(
            outcome=outcome_for(payload, pr_number),
            failing=failing,
            hold=hold,
            labels=labels,
            running=running,
        )
    lines = [MARKER, "", "## Rivet lander", "", banner, "", lead, ""]
    lines.extend(bullets)
    if bullets:
        lines.append("")
    lines.extend(
        [
            nxt,
            "",
            "The batching gate still blocks a third open pull request by the same "
            "author on the same files. Rivet does not bypass that gate. A "
            "`do-not-merge` label needs a comment that starts with `Hold:`.",
            "",
        ]
    )
    if title or pr_body or files:
        lines.append(reviewer_section(title=title, body=pr_body, files=files or [], hold=hold))
        lines.append("")
    return "\n".join(lines)


def comment_body(
    *,
    output: str,
    version: str,
    pin_ready: bool,
    pr_number: int | None = None,
    failing: list[dict] | None = None,
    hold: dict | None = None,
    labels: list[str] | None = None,
    running: int = 0,
    title: str = "",
    pr_body: str = "",
    files: list[dict] | None = None,
    secrets: list[str] | None = None,
) -> str:
    """Comment text. JSON, if any, sits in a collapsed block at the bottom."""
    visible = visible_body(
        output=output,
        version=version,
        pin_ready=pin_ready,
        pr_number=pr_number,
        failing=failing,
        hold=hold,
        labels=labels,
        running=running,
        title=title,
        pr_body=pr_body,
        files=files,
    )
    record = redact(output, secrets or []).strip()
    if not record:
        return visible
    fence = "json" if extract_json(record) else "text"
    gap = "" if visible.endswith("\n\n") else "\n"
    return (
        visible
        + gap
        + "\n".join(
            [
                "<details>",
                "<summary>Lander record</summary>",
                "",
                f"```{fence}",
                record,
                "```",
                "",
                "</details>",
                "",
            ]
        )
    )


def _failing_from_rollup(rollup: object) -> tuple[list[dict], int]:
    failing: list[dict] = []
    running = 0
    if not isinstance(rollup, list):
        return failing, running
    for check in rollup:
        if not isinstance(check, dict):
            continue
        conclusion = str(check.get("conclusion") or "").upper()
        status = str(check.get("status") or "").upper()
        if conclusion in _BAD_CHECKS:
            failing.append(
                {
                    "name": str(check.get("name") or "check"),
                    "url": str(check.get("detailsUrl") or ""),
                }
            )
        elif status in {"IN_PROGRESS", "QUEUED", "PENDING"} or not conclusion:
            running += 1
    return failing, running


def _hold_from_comments(labels: list[str], comments: object) -> dict | None:
    if "do-not-merge" not in labels:
        return None
    parsed_comments: list[Comment] = []
    if isinstance(comments, list):
        for item in comments:
            if not isinstance(item, dict):
                continue
            created = str(item.get("createdAt") or item.get("created_at") or "")
            if not created:
                continue
            parsed_comments.append(Comment(str(item.get("body") or ""), parse_time(created)))
    parsed, _err = resolve_hold(tuple(parsed_comments))
    if not parsed:
        return {
            "reason": "no Hold: comment yet",
            "releases": "someone who adds a Hold: comment",
            "review_by_text": "",
        }
    reason, releases, review_by, _at = parsed
    return {
        "reason": reason,
        "releases": releases,
        "review_by_text": short_date(review_by),
    }


def load_pr_context(repo: str, number: int) -> dict:
    result = _gh(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "title,body,files,labels,comments,statusCheckRollup",
        ]
    )
    empty = {
        "labels": [],
        "failing": [],
        "hold": None,
        "running": 0,
        "title": "",
        "body": "",
        "files": [],
    }
    if result.returncode != 0:
        return empty
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return empty
    labels = [
        str(item.get("name"))
        for item in (data.get("labels") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    failing, running = _failing_from_rollup(data.get("statusCheckRollup"))
    files = [
        {
            "path": str(item.get("path") or ""),
            "additions": int(item.get("additions") or 0),
            "deletions": int(item.get("deletions") or 0),
        }
        for item in (data.get("files") or [])
        if isinstance(item, dict)
    ]
    return {
        "labels": labels,
        "failing": failing,
        "hold": _hold_from_comments(labels, data.get("comments")),
        "running": running,
        "title": str(data.get("title") or ""),
        "body": str(data.get("body") or ""),
        "files": files,
    }


def _gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    # Fixed gh argv. The token comes from GH_TOKEN, not from these arguments.
    return subprocess.run(  # noqa: S603
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def upsert_comment(repo: str, pr: str, body: str) -> None:
    """Create or replace the sticky comment. Failures are printed, not raised."""
    import json

    listed = _gh(
        [
            "api",
            f"repos/{repo}/issues/{pr}/comments?per_page=100",
            "--jq",
            sticky_comment_jq(),
        ]
    )
    comment_id = (listed.stdout or "").strip()
    tmp = Path(os.environ.get("RUNNER_TEMP", "/tmp"))
    body_file = tmp / "rivet-warn-body.md"
    body_file.write_text(body, encoding="utf-8")
    if comment_id and comment_id != "null":
        payload = tmp / "rivet-warn-body.json"
        payload.write_text(json.dumps({"body": body}), encoding="utf-8")
        result = _gh(
            [
                "api",
                "-X",
                "PATCH",
                f"repos/{repo}/issues/comments/{comment_id}",
                "--input",
                str(payload),
            ]
        )
    else:
        result = _gh(["pr", "comment", pr, "--repo", repo, "--body-file", str(body_file)])
    if result.returncode != 0:
        print(result.stderr or result.stdout)
        print("::warning::could not post the Rivet warn comment")
    else:
        verb = "updated" if comment_id and comment_id != "null" else "posted"
        print(f"{verb} Rivet warn comment on {repo}#{pr}")


def _context() -> dict:
    raw = (os.environ.get("PR_NUMBER") or "").strip()
    number = int(raw) if raw.isdigit() else None
    repo = (os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    base = {
        "number": number,
        "labels": [],
        "failing": [],
        "hold": None,
        "running": 0,
        "title": "",
        "body": "",
        "files": [],
    }
    if number and repo and os.environ.get("GH_TOKEN"):
        loaded = load_pr_context(repo, number)
        loaded["number"] = number
        return loaded
    return base


def main() -> int:
    output_path = os.environ.get("LANDER_OUTPUT_FILE", "")
    output = ""
    if output_path and Path(output_path).is_file():
        output = Path(output_path).read_text(encoding="utf-8", errors="replace")
    secrets = [
        os.environ.get("RELEASE_FIX_WEBHOOK_KEY", ""),
        os.environ.get("RELEASE_FIX_WEBHOOK_URL", ""),
    ]
    version = os.environ.get("AXIOM_OS_LM_VERSION", "")
    pin_ready = os.environ.get("PIN_READY", "").lower() == "true"
    ctx = _context()
    redacted = redact(output, secrets)
    kwargs = {
        "output": redacted,
        "version": version,
        "pin_ready": pin_ready,
        "pr_number": ctx["number"],
        "failing": ctx["failing"],
        "hold": ctx["hold"],
        "labels": ctx["labels"],
        "running": ctx["running"],
        "title": ctx["title"],
        "pr_body": ctx["body"],
        "files": ctx["files"],
        "secrets": secrets,
    }
    visible = visible_body(
        output=redacted,
        version=version,
        pin_ready=pin_ready,
        pr_number=ctx["number"],
        failing=ctx["failing"],
        hold=ctx["hold"],
        labels=ctx["labels"],
        running=ctx["running"],
        title=ctx["title"],
        pr_body=ctx["body"],
        files=ctx["files"],
    )
    stdout_only = "--stdout-only" in sys.argv
    if stdout_only:
        print(visible)
        return 0
    body = comment_body(**kwargs)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(visible)
            if not visible.endswith("\n"):
                fh.write("\n")
    pr = (os.environ.get("PR_NUMBER") or "").strip()
    repo = (os.environ.get("REPO") or "").strip()
    if pr and repo and os.environ.get("GH_TOKEN"):
        upsert_comment(repo, pr, body)
    else:
        print(visible)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
