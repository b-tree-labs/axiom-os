# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Human-readable changelogs, out of the box.

Two sources, best first:

1. **Curated** — when the repo keeps a ``CHANGELOG.md`` with ``## [X.Y.Z]``
   headings, the sections for every version in the range are the story the
   author already wrote. Use them verbatim.
2. **Synthesized** — commit subjects in the range, grouped by conventional
   prefix (``feat:`` → New, ``fix:`` → Fixed, …) into a scannable digest.

Renderers: ``text`` (chat/terminal), ``markdown`` (PRs, release notes),
``json`` (machine consumers). A state file remembers the last ref a caller
shipped, so deploy pipelines diff "what actually reached the node" even when
versions are skipped.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_SECTION_ORDER = ("Features", "Bug fixes", "Releases", "Other changes")
_PREFIX_TO_SECTION = {
    "feat": "Features",
    "perf": "Features",
    "fix": "Bug fixes",
    "release": "Releases",
    "docs": "Other changes",
    "chore": "Other changes",
    "ci": "Other changes",
    "build": "Other changes",
    "refactor": "Other changes",
    "test": "Other changes",
    "style": "Other changes",
    "infra": "Other changes",
}
_PREFIX_RE = re.compile(r"^([a-z]+)(\([^)]*\))?!?:\s*(.+)$")
_HEADING_RE = re.compile(r"^## \[(?P<ver>[^\]]+)\]")


@dataclass
class Changelog:
    """A rendered-ready changelog between two refs."""

    from_ref: str | None
    to_ref: str
    commits: list[tuple[str, str, str]] = field(default_factory=list)  # (sha, subject, body)
    curated: list[str] = field(default_factory=list)  # raw CHANGELOG.md sections
    polished: list[tuple[str, list[str]]] | None = None  # AI-grouped, benefit-phrased

    @property
    def count(self) -> int:
        return len(self.commits)

    def sections(self) -> list[tuple[str, list[str]]]:
        """Grouped for humans: Features tell the story (subject + the commit
        body's first paragraph, where the benefit already lives), Bug fixes
        stay direct (the subject IS the fix), the rest keeps quiet."""
        buckets: dict[str, list[str]] = {}
        for _sha, subject, body in self.commits:
            m = _PREFIX_RE.match(subject)
            if m:
                title = _PREFIX_TO_SECTION.get(m.group(1), "Other changes")
                text = m.group(3)
            else:
                title, text = "Other changes", subject
            if title in ("Features", "Releases"):
                para = _first_paragraph(body)
                if para:
                    text = f"{text} — {para}"
            buckets.setdefault(title, []).append(text)
        return [(t, buckets[t]) for t in _SECTION_ORDER if t in buckets]


def _first_paragraph(body: str, limit: int = 240) -> str:
    """The commit body's opening paragraph, whitespace-collapsed and capped."""
    para = (body or "").strip().split("\n\n", 1)[0]
    para = " ".join(para.split())
    if len(para) > limit:
        para = para[: limit - 1].rsplit(" ", 1)[0] + "…"
    return para


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {result.stderr.strip() or result.returncode}")
    return result.stdout


def _ref_exists(repo: Path, ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"{ref}^{{commit}}"],
            capture_output=True,
            timeout=30,
        ).returncode
        == 0
    )


def _version_of(ref: str) -> str:
    return ref[1:] if ref.startswith("v") else ref


def curated_sections(changelog_file: Path, from_ref: str | None, to_ref: str) -> list[str]:
    """The author-written ``## [ver]`` sections covering (from, to].

    The file is assumed newest-first (the universal convention). We collect
    from the ``to`` version's heading down to — exclusive — the ``from``
    version's. Missing ``to`` heading → no curated story (caller falls back
    to synthesis). Any parse trouble degrades to the same fallback.
    """
    try:
        lines = changelog_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    to_ver = _version_of(to_ref)
    from_ver = _version_of(from_ref) if from_ref else None
    out: list[str] = []
    current: list[str] | None = None
    seen_to = False
    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            ver = m.group("ver")
            if ver == to_ver:
                seen_to = True
            elif seen_to and (ver == from_ver or from_ver is None):
                break
            if seen_to:
                if current:
                    out.append("\n".join(current).strip())
                current = [line]
                continue
        if current is not None:
            current.append(line)
    if current:
        out.append("\n".join(current).strip())
    return [s for s in out if s]


def build(
    repo: Path,
    *,
    from_ref: str | None,
    to_ref: str = "HEAD",
    limit: int = 20,
    changelog_file: str | Path | None = "auto",
) -> Changelog:
    """Collect the changelog between two refs (curated + synthesized)."""
    log_args = ["log", "--no-merges", f"--max-count={limit}", "--pretty=%h\x1f%s\x1f%b\x1e"]
    if from_ref and _DATE_RE.match(from_ref):
        log_args += [f"--since={from_ref}", to_ref]
    elif from_ref and _ref_exists(repo, from_ref):
        log_args.append(f"{from_ref}..{to_ref}")
    else:
        log_args.append(to_ref)
    raw = _git(repo, *log_args)
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if "\x1f" not in record:
            continue
        sha, subject, body = (record.split("\x1f", 2) + ["", ""])[:3]
        commits.append((sha.strip(), subject.strip(), body.strip()))
    curated: list[str] = []
    if changelog_file:
        path = repo / "CHANGELOG.md" if str(changelog_file) == "auto" else Path(changelog_file)
        if path.exists():
            curated = curated_sections(path, from_ref, to_ref)
    return Changelog(from_ref=from_ref, to_ref=to_ref, commits=list(commits), curated=curated)


def render(log: Changelog, fmt: str = "text") -> str:
    """Render for humans (text), documents (markdown), or machines (json)."""
    if fmt == "json":
        return json.dumps(
            {
                "since": log.from_ref,
                "from": log.from_ref,
                "to": log.to_ref,
                "count": log.count,
                "polished": log.polished is not None,
                "sections": [
                    {"title": t, "items": items} for t, items in (log.polished or log.sections())
                ],
                "commits": [{"sha": c[0], "subject": c[1], "body": c[2]} for c in log.commits],
                "curated": log.curated,
            },
            indent=2,
        )
    hop = f"{log.from_ref} → {log.to_ref}" if log.from_ref else log.to_ref
    n = f"{log.count} change{'s' if log.count != 1 else ''}"
    lines: list[str] = []
    if fmt == "markdown":
        lines.append(f"### {hop} ({n})")
        if log.polished:
            for title, items in log.polished:
                lines.append("")
                lines.append(f"**{title}**")
                lines.extend(f"- {i}" for i in items)
            return "\n".join(lines).strip()
        for section in log.curated:
            lines.append("")
            lines.append(section)
        if not log.curated:
            for title, items in log.sections():
                lines.append("")
                lines.append(f"**{title}**")
                lines.extend(f"- {i}" for i in items)
    else:
        lines.append(f"{hop} ({n})")
        if log.polished:
            for title, items in log.polished:
                lines.append("")
                lines.append(title)
                lines.extend(f"\u2022 {i}" for i in items)
            return "\n".join(lines).strip()
        if log.curated:
            for section in log.curated:
                lines.append("")
                # strip markdown heading chrome for chat surfaces
                lines.extend(
                    ln.lstrip("#").strip() if ln.startswith("#") else ln
                    for ln in section.splitlines()
                )
        else:
            for title, items in log.sections():
                lines.append("")
                lines.append(title)
                lines.extend(f"• {i}" for i in items)
    if not log.commits and not log.curated:
        lines.append("")
        lines.append(f"• no changes (redeploy of {log.to_ref})")
    return "\n".join(lines).strip()


def read_state(state_file: Path) -> str | None:
    try:
        return state_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_state(state_file: Path, ref: str) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(ref, encoding="utf-8")


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_POLISH_SYSTEM = "You rewrite a software changelog for the people who use the software."
_POLISH_PROMPT = """Rewrite these raw change lines as a user-facing changelog.

Rules:
1) Sort every change into "features" or "fixes" (leftovers go in "other").
2) features: a short title plus a 1\u20132 sentence description that explains what
   the person can DO now and which user type benefits (operator, researcher,
   student, admin, or developer).
3) fixes: ONE direct sentence \u2014 what was broken, and what works now.
4) Merge duplicate or related lines. NEVER invent a change that is not in the
   input. Plain language, no commit jargon, no ticket numbers.

Output STRICT JSON only, no prose, no code fences:
{{"features": [{{"title": "...", "description": "..."}}], "fixes": ["..."], "other": ["..."]}}

Raw change lines:
{raw}
"""


def polish(log: Changelog, *, gateway: object | None = None) -> list[tuple[str, list[str]]] | None:
    """AI cleanup: semantic grouping + benefit-phrased items.

    Uses the platform gateway (central tier resolution \u2014 never a hardcoded
    model). Returns None whenever the gateway is unavailable, errors, or returns
    malformed JSON \u2014 callers fall back to the deterministic ``sections()``
    grouping, so pipelines never block on a model.
    """
    if log.curated:
        raw_lines = list(log.curated)
    else:
        raw_lines = []
        for _sha, subject, body in log.commits:
            para = _first_paragraph(body, limit=300)
            raw_lines.append(f"{subject} :: {para}" if para else subject)
    if not raw_lines:
        return None
    if gateway is None:
        try:
            from axiom.infra.gateway import Gateway

            gateway = Gateway()
        except Exception:
            return None
    if not getattr(gateway, "available", False):
        return None
    try:
        response = gateway.complete(
            prompt=_POLISH_PROMPT.format(raw="\n".join(f"- {ln}" for ln in raw_lines)),
            system=_POLISH_SYSTEM,
            task="release",
            max_tokens=1200,
        )
        text = (getattr(response, "text", None) or "").strip()
        if text.startswith("```"):
            text = text.strip("`\n")
            text = text[text.index("{") :] if "{" in text else text
        data = json.loads(text)
        out: list[tuple[str, list[str]]] = []
        feats = [
            f"{str(f['title']).strip()} — {str(f['description']).strip()}"
            for f in data.get("features", [])
            if str(f.get("title", "")).strip() and str(f.get("description", "")).strip()
        ]
        if feats:
            out.append(("Features", feats))
        fixes = [str(i).strip() for i in data.get("fixes", []) if str(i).strip()]
        if fixes:
            out.append(("Bug fixes", fixes))
        other = [str(i).strip() for i in data.get("other", []) if str(i).strip()]
        if other:
            out.append(("Other changes", other))
        return out or None
    except Exception:
        return None


def recent_tags(repo: Path, *, to_ref: str = "HEAD", limit: int = 50) -> list[str]:
    """Tags reachable from ``to_ref``, newest release first.

    creatordate ties (lightweight tags cut in the same second) are broken by a
    numeric version sort, so v1.10.0 outranks v1.9.0 and same-second releases
    keep their real order.
    """

    def version_key(tag: str) -> tuple:
        nums = re.findall(r"\d+", tag)
        return tuple(int(n) for n in nums)

    try:
        raw = _git(
            repo, "tag", "--merged", to_ref, "--format=%(creatordate:unix)\x1f%(refname:short)"
        )
    except RuntimeError:
        return []
    rows = []
    for line in raw.splitlines():
        if "\x1f" not in line:
            continue
        ts, name = line.split("\x1f", 1)
        rows.append((int(ts.strip() or 0), version_key(name), name.strip()))
    rows.sort(reverse=True)
    return [name for _ts, _vk, name in rows][:limit]


# ---------------------------------------------------------------- last viewed


def _viewed_file(state_dir: Path) -> Path:
    return state_dir / "release" / "changelog-viewed.json"


def read_viewed(state_dir: Path, principal: str) -> str | None:
    """The ref this principal last viewed, or None."""
    try:
        data = json.loads(_viewed_file(state_dir).read_text(encoding="utf-8"))
        return (data.get(principal) or {}).get("ref") or None
    except Exception:
        return None


def write_viewed(state_dir: Path, principal: str, ref: str, *, now: str | None = None) -> None:
    """Record that this principal has seen everything up to ``ref``."""
    import datetime

    f = _viewed_file(state_dir)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data[principal] = {
        "ref": ref,
        "at": now or datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2), encoding="utf-8")
