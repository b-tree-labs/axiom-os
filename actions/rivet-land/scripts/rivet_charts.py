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


def check_counts(rollup: object) -> tuple[int, int, int]:
    """Passed, failing and running checks in a pull request's status rollup."""
    passed = failing = running = 0
    for check in rollup if isinstance(rollup, list) else []:
        if not isinstance(check, dict):
            continue
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


def _style(extra: str = "") -> str:
    light = "".join(f".f-{k}{{fill:{v[0]}}}.s-{k}{{stroke:{v[0]}}}" for k, v in {**STATUS, **_SURFACE}.items())
    dark = "".join(f".f-{k}{{fill:{v[1]}}}.s-{k}{{stroke:{v[1]}}}" for k, v in {**STATUS, **_SURFACE}.items())
    return (
        "<style>"
        f"text{{font-family:{_FONT}}}"
        f"{light}"
        f"@media (prefers-color-scheme: dark){{{dark}}}"
        "@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}"
        "@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}"
        ".grow{transform-box:fill-box;transform-origin:left;animation:grow .9s cubic-bezier(.2,.8,.2,1)}"
        ".pulse{animation:pulse 1.6s ease-in-out infinite}"
        "@media (prefers-reduced-motion: reduce){.grow,.pulse{animation:none}}"
        f"{extra}</style>"
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


def _svg(width: int, height: int, title: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        f"<title>{escape(title)}</title>{_style()}"
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" class="f-panel s-rule"/>'
        f"{body}</svg>"
    )


def status_card_svg(*, pr: int, group: str, passed: int, failing: int, running: int) -> str:
    glyph, word, tone = _BY_KEY.get(group, _BY_KEY["rivet"])
    if failing and group in {"rivet", "checks"}:
        tone = "bad"
    width, height = 680, 112
    pulse = ' class="pulse"' if group == "needs_you" else ""
    total = max(passed + failing + running, 1)
    bar_x, bar_w, bar_y = 380, 270, 46
    segs, x = [], bar_x
    for count, tone_k, anim in ((passed, "ok", ""), (failing, "bad", ""), (running, "info", " pulse")):
        if count:
            w = bar_w * count / total
            segs.append(f'<rect x="{x:.1f}" y="{bar_y}" width="{w:.1f}" height="16" class="f-{tone_k} grow{anim}"/>')
            x += w
    if not segs:
        segs.append(f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="16" class="f-well"/>')
    legend = (
        f'<text x="{bar_x}" y="{bar_y + 36}" font-size="13">'
        f'<tspan class="f-ok">✓ {passed} passed</tspan>'
        f'<tspan dx="14" class="f-bad">✕ {failing} failing</tspan>'
        f'<tspan dx="14" class="f-info">● {running} running</tspan></text>'
    )
    title = f"RIVET on #{pr}: {glyph} {word}. Checks: {passed} passed, {failing} failing, {running} running."
    body = (
        _mark(20, 24, 64)
        + f'<text x="100" y="44" font-size="13" font-weight="600" letter-spacing="1.5" class="f-ink3">RIVET · #{pr}</text>'
        + f'<text x="100" y="76" font-size="26" font-weight="700" class="f-{tone}"><tspan{pulse}>{glyph}</tspan> {escape(word)}</text>'
        + f'<text x="{bar_x}" y="{bar_y - 10}" font-size="12" class="f-ink3">Checks</text>'
        + f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="16" rx="4" class="f-well"/>'
        + "".join(segs)
        + legend
    )
    return _svg(width, height, title, body)


def backlog_svg(*, repo: str, counts: dict, this_group: str) -> str:
    rows = [(key, glyph, word, tone, int(counts.get(key) or 0)) for key, glyph, word, tone in GROUPS]
    total = sum(r[4] for r in rows)
    width, height = 680, 150
    bar_x, bar_w, bar_y = 20, 640, 62
    title = f"Open pull requests in {repo}: {total} open. " + "; ".join(
        f"{g} {w} {n}" for _k, g, w, _t, n in rows if n
    ) + "."
    parts = [
        '<defs><pattern id="held" width="6" height="6" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)"><rect width="6" height="6" class="f-well"/>'
        '<rect width="2.5" height="6" class="f-none"/></pattern></defs>',
        f'<text x="20" y="30" font-size="15" font-weight="700" class="f-ink">{escape(repo)}</text>'
        f'<text x="660" y="30" font-size="13" text-anchor="end" class="f-ink3">{total} open</text>',
        f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="22" rx="6" class="f-well"/>',
    ]
    x = float(bar_x)
    for key, _g, _w, tone, n in rows:
        if not n:
            continue
        w = bar_w * n / max(total, 1)
        anim = "grow pulse" if key == "needs_you" else "grow"
        # With the author is held, so it is a neutral hatch (DC-11), not a second grey.
        fill = ' fill="url(#held)"' if key == "author" else ""
        cls = anim if fill else f"f-{tone} {anim}"
        parts.append(f'<rect x="{x:.1f}" y="{bar_y}" width="{max(w - 2, 1):.1f}" height="22" class="{cls}"{fill}/>')
        if key == this_group:
            mid = x + w / 2
            parts.append(
                f'<path d="M{mid - 6:.1f} {bar_y - 9} L{mid + 6:.1f} {bar_y - 9} L{mid:.1f} {bar_y - 2} Z" class="f-ink"/>'
                f'<text x="{mid:.1f}" y="{bar_y - 12}" font-size="11" text-anchor="middle" class="f-ink3">this pull request</text>'
            )
        x += w
    lx, ly = 20.0, bar_y + 46
    legend = []
    for key, glyph, word, tone, n in rows:
        if not n:
            continue
        label = f"{glyph} {word} {n}"
        span = 7.6 * len(label) + 22
        if lx + span > width - 16:
            lx, ly = 20.0, ly + 22
        weight = 700 if key == "needs_you" else 500
        legend.append(f'<text x="{lx:.0f}" y="{ly}" font-size="13" class="f-{tone}" font-weight="{weight}">{escape(label)}</text>')
        lx += span
    if not legend:
        legend.append(f'<text x="20" y="{ly}" font-size="13" class="f-ink3">No open pull requests.</text>')
    parts.extend(legend)
    return _svg(width, height, title, "".join(parts))


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
                running: int, counts: dict, publish_fn=publish) -> str:
    """The comment's chart block, or "" when nothing could be published."""
    glyph, word, _tone = _BY_KEY.get(group, _BY_KEY["rivet"])
    card = status_card_svg(pr=pr, group=group, passed=passed, failing=failing, running=running)
    chart = backlog_svg(repo=repo, counts=counts, this_group=group)
    status = publish_fn(repo, chart_path(pr, "status", card), card)
    backlog = publish_fn(repo, chart_path(pr, "backlog", chart), chart)
    lines = []
    if status:
        lines.append(embed_markdown(status, alt=f"RIVET: {glyph} {word}; {passed} passed, {failing} failing, {running} running",
                                    link=f"https://github.com/{repo}/pull/{pr}/checks"))
    if backlog:
        total = sum(int(counts.get(k) or 0) for k, *_ in GROUPS)
        lines.append(embed_markdown(backlog, alt=f"{total} open pull requests by who acts next",
                                    link=f"https://github.com/{repo}/pulls"))
    return "\n".join(lines) + ("\n" if lines else "")
