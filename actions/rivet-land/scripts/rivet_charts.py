"""Branded charts for RIVET's sticky comment, as SVG. Standard library only.

Two charts lead the comment, so a person reads the decision before the prose:

- the status card: who acts next on this pull request (a glyph and a word, DC-11),
  and its checks as passed, failing and running;
- the backlog: the repository's open pull requests by who acts next, with this
  pull request marked.

GitHub shows a comment image as a plain image, so there is no hover or click inside
a chart. What does work there, and is used: CSS animation (bars grow in; what waits
on a person pulses), the reader's light or dark scheme, and reduced motion.

Colours: Axiom's accent (burnt orange, `packages/axiom-design-tokens`) marks the brand
only. Status colours are appkit's status tokens (`frontend/src/tokens.css`, ADR-226),
light then dark. The accent is never a status.

Charts are kept on the `rivet-charts` branch of the same repository, so a member's
browser loads them and nothing leaves the repository. No workflow runs on that branch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from html import escape

ACCENT = "#bf5700"
CHART_BRANCH = "rivet-charts"

#: appkit status tokens: (light, dark).
STATUS = {
    "ok": ("#0d7762", "#56d4a6"),
    "warn": ("#8a6d00", "#e3d23a"),
    "bad": ("#a91830", "#ff5c6a"),
    "info": ("#1f6f8b", "#e0b0ff"),
    "none": ("#6b737c", "#8b939b"),
}
#: appkit surface and ink tokens: (light, dark).
_SURFACE = {"panel": ("#ffffff", "#1b1d21"), "ink": ("#1d1d1b", "#ecebe6"),
            "ink3": ("#76746d", "#9a988f"), "rule": ("#e3e0d8", "#2d3036"),
            "well": ("#f8f7f4", "#171a1e")}

#: Who acts next. Order is the order a person reads: what needs them first.
GROUPS = (
    ("needs_you", "⏳", "Needs you", "warn"),
    ("rivet", "●", "RIVET is handling", "info"),
    ("checks", "?", "Waiting on checks", "none"),
    ("author", "‖", "With the author", "none"),
    ("ready", "✓", "Ready to land", "ok"),
)
_BY_KEY = {key: (glyph, word, tone) for key, glyph, word, tone in GROUPS}
_BY_KEY["landed"] = ("✓", "Landed", "ok")

_PERSON_REASONS = ("waiting on ", "protected", "changes requested", "conflict",
                   "do-not-merge", "hold", "try-cap", "round cap", "raci-skip",
                   "approver not on the roster", "approval")


def group_for_outcome(item: dict) -> str:
    """Who acts next on one lander outcome."""
    status = str(item.get("status") or "")
    reason = str(item.get("reason") or "").lower()
    if status == "landed":
        return "landed"
    if reason == "draft":
        return "author"
    if status == "escalated" or reason.startswith(_PERSON_REASONS):
        return "needs_you"
    if reason in {"checks not green", "checks_unknown", "pending", "behind"}:
        return "checks"
    if status == "invalidated":
        return "checks"
    return "rivet"


def _empty() -> dict:
    return {key: 0 for key in (*(g[0] for g in GROUPS), "landed")}


def counts_from_outcomes(payload: dict | None) -> dict:
    counts = _empty()
    for item in (payload or {}).get("outcomes") or []:
        if isinstance(item, dict):
            counts[group_for_outcome(item)] += 1
    return counts


_TEXT_HEADERS = {
    "ready to land": "ready",
    "needs a person": "needs_you",
    "rivet is handling": "rivet",
    "with the author": "author",
    "waiting on ci": "checks",
}


def counts_from_text(report: str) -> dict:
    """Counts from the lander's text report (`land --format text`)."""
    counts = _empty()
    for line in report.splitlines():
        match = re.match(r"^([a-zA-Z ]+) \((\d+)\):\s*$", line.strip())
        if match and match.group(1).lower() in _TEXT_HEADERS:
            counts[_TEXT_HEADERS[match.group(1).lower()]] = int(match.group(2))
    return counts


_REPORT_KEYS = {
    "needs_human": "needs_you",
    "held_for_owner_review": "needs_you",
    "rivet_handling": "rivet",
    "waiting_on_ci": "checks",
    "with_author": "author",
    "ready": "ready",
}


def counts_from_report(report: dict | None) -> dict:
    """Counts from the lander's JSON report (`BacklogReport.to_dict`)."""
    counts = _empty()
    for key, group in _REPORT_KEYS.items():
        counts[group] += len((report or {}).get(key) or [])
    return counts


def group_from_report(report: dict | None, number: int) -> str:
    for key, group in _REPORT_KEYS.items():
        for item in (report or {}).get(key) or []:
            if isinstance(item, dict) and item.get("number") == number:
                return group
    return "checks"


def _latest_rows(rollup: object) -> list[dict]:
    """Each check's newest run on the head. A newer run of the same check replaces an older
    one (a cancelled first run beside a green second is one green check, not a failure)."""
    best: dict[tuple[str, str], tuple[str, dict]] = {}
    order: list[tuple[str, str]] = []
    for index, check in enumerate(rollup if isinstance(rollup, list) else []):
        if not isinstance(check, dict):
            continue
        key = (str(check.get("workflowName") or ""), str(check.get("name") or check.get("context") or index))
        status = str(check.get("status") or "").upper()
        newest = "9999" if status in {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED"} else str(
            check.get("completedAt") or check.get("startedAt") or ""
        )
        if key not in best:
            order.append(key)
        if key not in best or newest >= best[key][0]:
            best[key] = (newest, check)
    return [best[k][1] for k in order]


def check_counts(rollup: object) -> tuple[int, int, int]:
    """Passed, failing and running checks, each check judged by its latest run."""
    passed = failing = running = 0
    for check in _latest_rows(rollup):
        conclusion = str(check.get("conclusion") or check.get("state") or "").upper()
        status = str(check.get("status") or "").upper()
        if conclusion == "SUCCESS":
            passed += 1
        elif conclusion in {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"}:
            failing += 1
        elif conclusion in {"NEUTRAL", "SKIPPED", "STALE"}:
            continue
        elif status in {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED"} or not conclusion:
            running += 1
    return passed, failing, running


def group_from_text(report: str, number: int) -> str:
    """This pull request's group in the text report. Not listed yet: its checks are running."""
    section = ""
    for line in report.splitlines():
        match = re.match(r"^([a-zA-Z ]+) \((\d+)\):\s*$", line.strip())
        if match:
            section = _TEXT_HEADERS.get(match.group(1).lower(), "")
            continue
        if section and re.match(rf"^\s+#{number}\b", line):
            return section
    return "checks"


# -- drawing -----------------------------------------------------------------------

_FONT = "system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _style() -> str:
    light = "".join(f".f-{k}{{fill:{v[0]}}}" for k, v in {**STATUS, **_SURFACE}.items())
    dark = "".join(f".f-{k}{{fill:{v[1]}}}" for k, v in {**STATUS, **_SURFACE}.items())
    stroke = f".s-rule{{stroke:{_SURFACE['rule'][0]}}}"
    stroke_dark = f".s-rule{{stroke:{_SURFACE['rule'][1]}}}"
    return (
        "<style>"
        f"text{{font-family:{_FONT}}}{light}{stroke}"
        f"@media (prefers-color-scheme: dark){{{dark}{stroke_dark}}}"
        "</style>"
    )


def _mark(x: float, y: float, size: float) -> str:
    """Axiom's mark: the burnt-orange rounded square with the A (webgate favicon)."""
    s = size / 64
    return (
        f'<g transform="translate({x},{y}) scale({s:.4f})">'
        f'<rect x="2" y="2" width="60" height="60" rx="16" fill="{ACCENT}"/>'
        '<path d="M20 43 L32 20 L44 43" fill="none" stroke="#fff" stroke-width="5.5" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M26 35 H38" fill="none" stroke="#fff" stroke-width="5.5" stroke-linecap="round"/>'
        "</g>"
    )


#: One strip, one size: fixed height, two type sizes (14 for the state, 12 for the rest),
#: no animation. It states a fact about this pull request and nothing that is not true now.
STRIP_W, STRIP_H = 640, 56


def _not_run_strip_svg(pr: int) -> str:
    """The strip for a head whose required checks have no run that counts. No counts at all:
    a pass count that leaves the required checks out is the lie this exists to stop (#2354)."""
    title = f"RIVET on #{pr}: tests: not run. No required check has a run that counts."
    body = (
        f'<rect x="0.5" y="0.5" width="{STRIP_W - 1}" height="{STRIP_H - 1}" rx="8" class="f-panel s-rule" stroke-width="1"/>'
        + _mark(14, 14, 28)
        + '<text x="54" y="25" font-size="14" font-weight="600" class="f-bad">tests: not run</text>'
        + f'<text x="54" y="43" font-size="12" class="f-ink3">RIVET · #{pr}</text>'
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{STRIP_W}" height="{STRIP_H}" '
        f'viewBox="0 0 {STRIP_W} {STRIP_H}" role="img" aria-label="{escape(title)}">'
        f"<title>{escape(title)}</title>{_style()}{body}</svg>"
    )


def status_strip_svg(*, pr: int, word: str, tone: str, passed: int, failing: int, running: int,
                     not_run: bool = False) -> str:
    if not_run:
        return _not_run_strip_svg(pr)
    total = max(passed + failing + running, 1)
    bar_x, bar_w, bar_y, bar_h = 300, 140, 22, 10
    segs, x = [], float(bar_x)
    for count, kind in ((passed, "ok"), (failing, "bad"), (running, "info")):
        if count:
            w = bar_w * count / total
            segs.append(f'<rect x="{x:.1f}" y="{bar_y}" width="{w:.1f}" height="{bar_h}" class="f-{kind}"/>')
            x += w
    if not segs:
        segs.append(f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" class="f-well"/>')
    title = f"RIVET on #{pr}: {word}. Checks: {passed} passed, {failing} failing, {running} running."
    shown = [(n, kind, label) for n, kind, label in ((passed, "ok", "passed"), (failing, "bad", "failing"),
                                                       (running, "info", "running")) if n]
    spans = "".join(f'<tspan dx="{0 if i == 0 else 12}" class="f-{kind}">{n} {label}</tspan>'
                    for i, (n, kind, label) in enumerate(shown)) or '<tspan class="f-ink3">no checks yet</tspan>'
    counts = f'<text x="{bar_x + bar_w + 14}" y="{bar_y + 9}" font-size="12">{spans}</text>'
    body = (
        f'<rect x="0.5" y="0.5" width="{STRIP_W - 1}" height="{STRIP_H - 1}" rx="8" class="f-panel s-rule" stroke-width="1"/>'
        + _mark(14, 14, 28)
        + f'<text x="54" y="25" font-size="14" font-weight="600" class="f-{tone}">{escape(word)}</text>'
        + f'<text x="54" y="43" font-size="12" class="f-ink3">RIVET · #{pr}</text>'
        + f'<text x="{bar_x}" y="{bar_y - 5}" font-size="12" class="f-ink3">Checks</text>'
        + f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="3" class="f-well"/>'
        + "".join(segs)
        + counts
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{STRIP_W}" height="{STRIP_H}" '
        f'viewBox="0 0 {STRIP_W} {STRIP_H}" role="img" aria-label="{escape(title)}">'
        f"<title>{escape(title)}</title>{_style()}{body}</svg>"
    )


# -- publishing --------------------------------------------------------------------


def chart_url(repo: str, path: str) -> str:
    return f"https://github.com/{repo}/blob/{CHART_BRANCH}/{path}?raw=true"


def embed_markdown(url: str, *, alt: str, link: str = "") -> str:
    img = f'<img src="{escape(url)}" alt="{escape(alt)}" width="680">'
    return f'<a href="{escape(link)}">{img}</a>' if link else img


def _gh(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], input=stdin, check=False, capture_output=True, text=True)  # noqa: S603


def _ensure_branch(repo: str) -> bool:
    if _gh(["api", f"repos/{repo}/git/ref/heads/{CHART_BRANCH}"]).returncode == 0:
        return True
    readme = "RIVET keeps the charts for its pull request comments here. Nothing runs on this branch.\n"
    tree = _gh(["api", "-X", "POST", f"repos/{repo}/git/trees", "--input", "-"],
               json.dumps({"tree": [{"path": "README.md", "mode": "100644", "type": "blob", "content": readme}]}))
    if tree.returncode != 0:
        return False
    commit = _gh(["api", "-X", "POST", f"repos/{repo}/git/commits", "--input", "-"],
                 json.dumps({"message": "rivet-charts: start", "tree": json.loads(tree.stdout)["sha"], "parents": []}))
    if commit.returncode != 0:
        return False
    ref = _gh(["api", "-X", "POST", f"repos/{repo}/git/refs", "--input", "-"],
              json.dumps({"ref": f"refs/heads/{CHART_BRANCH}", "sha": json.loads(commit.stdout)["sha"]}))
    return ref.returncode == 0 or "already exists" in (ref.stdout + ref.stderr)


def chart_path(pr: int, kind: str, svg: str) -> str:
    """Named by content, so an unchanged chart is never written twice."""
    digest = hashlib.sha256(svg.encode("utf-8")).hexdigest()[:16]
    return f"charts/pr-{pr}/{kind}-{digest}.svg"


def publish(repo: str, path: str, svg: str) -> str:
    """Put one chart on the chart branch. Returns its URL, or "" when it could not."""
    if _gh(["api", f"repos/{repo}/contents/{path}?ref={CHART_BRANCH}", "--jq", ".sha"]).returncode == 0:
        return chart_url(repo, path)
    if not _ensure_branch(repo):
        return ""
    payload = {"message": f"rivet-charts: {path}", "branch": CHART_BRANCH,
               "content": base64.b64encode(svg.encode("utf-8")).decode("ascii")}
    for _ in range(3):
        put = _gh(["api", "-X", "PUT", f"repos/{repo}/contents/{path}", "--input", "-"], json.dumps(payload))
        if put.returncode == 0:
            return chart_url(repo, path)
        if _gh(["api", f"repos/{repo}/contents/{path}?ref={CHART_BRANCH}", "--jq", ".sha"]).returncode == 0:
            return chart_url(repo, path)
    return ""


def chart_block(*, repo: str, pr: int, head_sha: str, group: str, passed: int, failing: int,
                running: int, counts: dict | None = None, publish_fn=publish, word: str = "",
                tone: str = "", link: str = "", not_run: bool = False) -> str:
    """The note's one status image, or "" when it could not be published.

    The repository-wide backlog is not drawn here: it was a snapshot frozen at the note's
    last edit, repeated on every pull request. It lives in the run summary and the owner
    issue, which are current. ``counts`` is accepted and ignored for older callers.
    """
    glyph, default_word, default_tone = _BY_KEY.get(group, _BY_KEY["rivet"])
    word = word or default_word
    tone = tone or default_tone
    strip = status_strip_svg(pr=pr, word=word, tone=tone, passed=passed, failing=failing, running=running,
                             not_run=not_run)
    url = publish_fn(repo, chart_path(pr, "status", strip), strip)
    if not url:
        return ""
    alt = (
        "RIVET: tests: not run"
        if not_run
        else f"RIVET: {word}; {passed} passed, {failing} failing, {running} running"
    )
    return embed_markdown(url, alt=alt, link=link or f"https://github.com/{repo}/pull/{pr}/checks") + "\n"
