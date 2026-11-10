# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.publish`` — write the status page and a Gantt PNG to a wiki.

The render target is a **wiki target**: a git repository holding Markdown
pages, named in the data file as ``program.publish.wiki``::

    {"repo": "<owner>/<name>", "page": "Program-Status", "host": "github.com"}

The publisher renders two files — ``<page>.md`` (the status page: one
section per lane, each item with owner, dates, status, delivery, percent and
links, plus the open proposals waiting for an owner) and ``<page>-gantt.png``
— clones ``https://<host>/<repo>.wiki.git``, writes them, and commits and
pushes **only when something changed**. A second publish of the same data
makes no commit.

Authentication is a GitHub App installation token minted for this one push,
with ``contents: write`` on the wiki's repository and nothing else (the
tracker reads use a separate, read-only token). The token reaches git through
the environment as an HTTP header (``GIT_CONFIG_*``), never on argv and never
in the remote URL, and any git error text is scrubbed of it before it is
reported. The system credential helper is disabled for every git call so a
publish can never block on an interactive keychain prompt.

``dry_run`` renders into ``out`` and touches no repository.
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..model import ProgramData, ProgramError, load_program
from ._gantt import render_gantt_png
from .status import _item_view

TARGETS = ("wiki",)
DEFAULT_PAGE = "Program-Status"
#: The commit identity a wiki publish uses (the agent, not a person).
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "CLERK",
    "GIT_AUTHOR_EMAIL": "clerk@noreply.invalid",
    "GIT_COMMITTER_NAME": "CLERK",
    "GIT_COMMITTER_EMAIL": "clerk@noreply.invalid",
}


# ---------------------------------------------------------------------------
# The Markdown status page
# ---------------------------------------------------------------------------


def _md(text: Any) -> str:
    """Escape the characters that would turn data into Markdown structure."""
    out = str(text)
    for ch in ("\\", "|", "*", "_", "`", "[", "]", "<", ">"):
        out = out.replace(ch, "\\" + ch)
    return out


def _dates(view: dict[str, Any]) -> str:
    dates = view["dates"]
    if dates.get("date"):
        return str(dates["date"])
    if dates.get("start") or dates.get("end"):
        return f"{dates.get('start') or '?'} to {dates.get('end') or '?'}"
    return ""


def render_wiki_markdown(data: ProgramData, *, gantt_file: str | None) -> str:
    program = data.program
    lines = [f"# {_md(program.get('name', program.get('id', 'Program')))}", ""]
    meta = []
    if program.get("as_of"):
        meta.append(f"As of {program['as_of']}")
    if program.get("deputy"):
        meta.append(f"deputy {program['deputy']}")
    if meta:
        lines += [_md(" · ".join(meta)), ""]
    if gantt_file:
        lines += [f"![Program schedule as a Gantt chart]({gantt_file})", ""]

    by_id = {item.get("id"): item for item in data.schedule if isinstance(item, dict)}
    lanes = [(lane["id"], lane.get("name", lane["id"])) for lane in data.lanes if isinstance(lane, dict)]
    laned = {lane_id for lane_id, _ in lanes}
    groups = [*lanes, (None, "Unlaned")]
    for lane_id, name in groups:
        members = [
            item for item in data.schedule
            if isinstance(item, dict)
            and (item.get("lane") == lane_id if lane_id is not None else item.get("lane") not in laned)
        ]
        if lane_id is None and not members:
            continue
        lines += [f"## {_md(name)}", ""]
        if not members:
            lines += ["_No items._", ""]
            continue
        lines += ["| Item | Owner | Dates | Status | Delivery | % | Links |", "|---|---|---|---|---|---|---|"]
        for entry in members:
            view = _item_view(entry, data, fmt="full")
            raw = by_id.get(view["id"], {})
            links = []
            for link in view["links"]:
                if link["kind"] == "url":
                    links.append(f"[{_md(link['field'])}]({link['href']})")
                elif link["kind"] == "tracker":
                    links.append(_md(f"{link['host']}/{link['project']}#{link['ref']}"))
            tracker = raw.get("tracker") if isinstance(raw.get("tracker"), dict) else {}
            linked = tracker.get("linked") if isinstance(tracker.get("linked"), dict) else None
            if linked and linked.get("url"):
                state = linked.get("state") or "unverified"
                links.append(f"[{_md(linked.get('system', 'linked'))}: {_md(state)}]({linked['url']})")
            evidence = (raw.get("evidence") or {}).get("work") if isinstance(raw.get("evidence"), dict) else None
            if evidence:
                links.append(f"{len(evidence)} work item{'s' if len(evidence) != 1 else ''}")
            cells = [
                f"**{_md(view['label'])}** `{view['id']}`",
                _md(view["owner"] or ""),
                _md(_dates(view)),
                _md(view["status"] or ""),
                _md(raw.get("delivery") or ""),
                "" if view["pct"] is None else str(view["pct"]),
                " · ".join(links),
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    proposals = [
        f for f in ((data.raw.get("capture") or {}).get("findings") or [])
        if isinstance(f, dict) and f.get("kind") in ("progress_proposal", "date_proposal")
    ]
    if proposals:
        lines += ["## Proposals for owners", ""]
        for f in proposals:
            who = f.get("owner") or "unowned"
            lines.append(
                f"- `{f.get('item')}` {_md(f.get('field'))}: {_md(f.get('current'))} → "
                f"{_md(f.get('proposed'))} ({_md(who)}; {_md(f.get('basis') or 'no basis')})"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_files(data: ProgramData, page: str) -> tuple[dict[str, bytes], list[str]]:
    """The files a wiki publish writes, and any notes (a missing optional
    renderer is said, not hidden)."""
    notes: list[str] = []
    gantt_name = f"{page}-gantt.png"
    png = render_gantt_png(data)
    if png is None:
        notes.append("Gantt image skipped: matplotlib is not installed (the 'math' extra)")
    files = {f"{page}.md": render_wiki_markdown(data, gantt_file=gantt_name if png else None).encode("utf-8")}
    if png is not None:
        files[gantt_name] = png
    return files, notes


# ---------------------------------------------------------------------------
# The wiki target (git)
# ---------------------------------------------------------------------------


class PublishError(ProgramError):
    """A publish step failed. The message never carries the token."""


def _auth_env(token: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    return env


def _git(args: list[str], *, cwd: Path | None, env: dict[str, str], secret: str | None) -> str:
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", "-c", "credential.helper=", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()[-1:] or [""]
        text = detail[0]
        if secret:
            text = text.replace(secret, "***")
        raise PublishError(f"git {args[0]} failed (exit {result.returncode}): {text}")
    return result.stdout


def publish_to_git(
    files: dict[str, bytes],
    *,
    remote: str,
    token: str | None,
    message: str,
) -> dict[str, Any]:
    """Write ``files`` into the repository at ``remote`` and push, committing
    only when the tree changed. Returns ``{"committed", "sha", "files"}``."""
    env = _auth_env(token)
    basic = None
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    with tempfile.TemporaryDirectory(prefix="axiom-wiki-") as tmp:
        work = Path(tmp) / "wiki"
        _git(["clone", "--depth", "1", "--quiet", remote, str(work)], cwd=None, env=env, secret=basic)
        has_head = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=work, capture_output=True, check=False
        ).returncode == 0
        if not has_head:
            _git(["checkout", "--quiet", "-B", "master"], cwd=work, env=env, secret=basic)
        for name, content in files.items():
            (work / name).write_bytes(content)
        _git(["add", "--", *files], cwd=work, env=env, secret=basic)
        changed = subprocess.run(  # noqa: S603
            ["git", "diff", "--cached", "--quiet"], cwd=work, capture_output=True, check=False
        ).returncode != 0
        if not changed:
            sha = _git(["rev-parse", "HEAD"], cwd=work, env=env, secret=basic).strip()
            return {"committed": False, "sha": sha, "files": sorted(files)}
        _git(["commit", "--quiet", "-m", message], cwd=work, env={**env, **_COMMIT_ENV}, secret=basic)
        branch = _git(["symbolic-ref", "--short", "HEAD"], cwd=work, env=env, secret=basic).strip()
        _git(["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=work, env=env, secret=basic)
        sha = _git(["rev-parse", "HEAD"], cwd=work, env=env, secret=basic).strip()
        return {"committed": True, "sha": sha, "files": sorted(files)}


def _wiki_token(program: dict[str, Any], repo: str, ctx: SkillContext) -> str:
    """A one-push installation token: ``contents: write`` on the wiki's
    repository only."""
    from ._github_app import GitHubAppConfig, GitHubAppTokenProvider, vault_key_loader

    config = GitHubAppConfig.from_raw(program.get("github_app"))
    if config is None:
        raise PublishError("publishing to a wiki needs program.github_app (no App is declared)")
    provider = GitHubAppTokenProvider(
        config,
        key_loader=vault_key_loader(config.credential, ctx.state_dir),
        permissions={"contents": "write", "metadata": "read"},
        repositories=[repo.split("/", 1)[1]],
    )
    return provider.token()


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    data_path = Path(params.get("data") or ctx.state_dir / "program" / "data.json")
    target = params.get("target") or "wiki"
    if target not in TARGETS:
        return SkillResult(ok=False, errors=[f"unknown target {target!r}; one of {', '.join(TARGETS)}"])
    try:
        data = load_program(data_path, require_listed_owners=False)
    except ProgramError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    wiki = ((data.program.get("publish") or {}).get("wiki")) or {}
    repo = wiki.get("repo") if isinstance(wiki, dict) else None
    if not isinstance(repo, str) or repo.count("/") != 1:
        return SkillResult(ok=False, errors=["program.publish.wiki.repo ('owner/name') is not declared"])
    page = str(wiki.get("page") or DEFAULT_PAGE)
    host = str(wiki.get("host") or "github.com")

    files, notes = render_files(data, page)

    if params.get("dry_run"):
        out_dir = Path(params.get("out") or ctx.state_dir / "program" / "wiki")
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (out_dir / name).write_bytes(content)
        return SkillResult(
            ok=True,
            value={"target": target, "repo": repo, "dry_run": True, "out": str(out_dir), "files": sorted(files), "notes": notes},
            actions_taken=[f"rendered {len(files)} file(s) into {out_dir}"],
        )

    remote = params.get("_remote")  # test seam: a local repository path
    try:
        token = params.get("_token") if remote else _wiki_token(data.program, repo, ctx)
        result = publish_to_git(
            files,
            remote=remote or f"https://{host}/{repo}.wiki.git",
            token=token,
            message=f"Update {page} from the program data ({data.program.get('as_of') or 'no as_of'})",
        )
    except ProgramError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    except Exception as exc:  # noqa: BLE001 — credential or transport; no detail that could carry a secret
        return SkillResult(ok=False, errors=[f"wiki publish failed ({type(exc).__name__})"])

    actions = [f"committed {result['sha'][:12]} to {repo} wiki"] if result["committed"] else []
    return SkillResult(
        ok=True,
        value={"target": target, "repo": repo, "page": page, **result, "notes": notes},
        actions_taken=actions,
    )


__all__ = ["TARGETS", "render_wiki_markdown", "render_files", "publish_to_git", "run"]
