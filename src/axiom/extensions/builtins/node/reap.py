# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Reaping finished git worktrees, safely (KEEL, phase LE2).

The hygiene detector (``hygiene.worktrees``) says a worktree *looks* finished.
That is not proof it is safe to delete: its S3 signal fires on any HEAD that is
an ancestor of main, and S2 fires on any branch missing from origin. This module
is the stricter gate that sits on top. A worktree is removable only when ALL hold:

  * its commits are on the default branch, or its PR merged (never "closed");
  * ``git status`` is clean, with no untracked content;
  * no process has its working directory inside it (unknown means blocked);
  * it is not locked, not the base checkout, not a checkout of the default branch;
  * no editable install points into it;
  * its last git activity is older than ``min_age_hours``.

Git-ignored state is not a blocker, but it is never silently destroyed: whatever
is ignored beyond regenerable caches (virtualenvs, bytecode, tool caches,
node_modules, testmon data) is archived to a tar.gz under the user state
directory, with a manifest line, BEFORE anything is removed. If the archive
fails, nothing is removed. Branches are always kept. Removal is plain
``git worktree remove`` with no ``--force``.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.hygiene import worktrees as wtmod

ARCHIVE_DIR = "worktree-archives"
MANIFEST = "manifest.jsonl"
CONFIG_FILE = "worktree-reap.json"

#: Ignored paths that are regenerable: removed with the worktree, never archived.
REGENERABLE_NAMES: frozenset[str] = frozenset(
    {
        ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", ".tox", ".nox", ".cache", ".DS_Store", ".testmondata",
        ".hypothesis", ".ipynb_checkpoints", ".eggs",
    }
)
REGENERABLE_GLOBS: tuple[str, ...] = ("*.pyc", "*.pyo", ".testmondata*", ".coverage*", "*.egg-info")


@dataclass
class ReapConfig:
    min_age_hours: float = 24.0
    batch: int = 5
    retention_days: float = 30.0
    max_archive_mb: float = 2048.0
    roots: list[str] = field(default_factory=list)


def load_config(state_dir: Path, **overrides: Any) -> ReapConfig:
    cfg = ReapConfig()
    try:
        raw = json.loads((Path(state_dir) / CONFIG_FILE).read_text())
    except (OSError, ValueError):
        raw = {}
    for source in (raw if isinstance(raw, dict) else {}, overrides):
        for k, v in source.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, type(getattr(cfg, k))(v))
    return cfg


def _git(args: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    return wtmod._run(["git", *args], cwd=cwd, timeout=timeout)


# ---- facts ---------------------------------------------------------------------------


def process_cwds() -> list[Path] | None:
    """Working directory of every process we can see, or None when we cannot tell.

    ``None`` blocks every removal: not knowing is not the same as nobody being there.
    """
    try:
        r = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-F", "n"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        # lsof exits 1 when some processes are unreadable but still prints the rest.
        if r.stdout.strip():
            return [Path(ln[1:]) for ln in r.stdout.splitlines() if ln.startswith("n/")]
    except (OSError, subprocess.SubprocessError):
        pass
    proc = Path("/proc")
    if proc.is_dir():
        out: list[Path] = []
        for p in proc.glob("[0-9]*/cwd"):
            try:
                out.append(Path(os.readlink(p)))
            except OSError:
                continue
        if out:
            return out
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def last_activity(wt: Path) -> float | None:
    """Most recent git activity: HEAD/index/reflog mtimes and the HEAD commit time."""
    rc, out = _git(["rev-parse", "--absolute-git-dir"], wt)
    times: list[float] = []
    if rc == 0 and out.strip():
        gd = Path(out.strip())
        for n in ("HEAD", "index", "logs/HEAD"):
            try:
                times.append((gd / n).stat().st_mtime)
            except OSError:
                pass
    rc, out = _git(["log", "-1", "--format=%ct", "HEAD"], wt)
    if rc == 0 and out.strip().isdigit():
        times.append(float(out.strip()))
    return max(times) if times else None


def ignored_entries(wt: Path) -> list[str] | None:
    """Top-level git-ignored paths (directories collapsed), relative to ``wt``."""
    rc, out = _git(
        ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
        wt, timeout=120,
    )
    if rc != 0:
        return None
    return [e.rstrip("/") for e in out.split("\0") if e]


def is_regenerable(rel: str) -> bool:
    parts = Path(rel).parts
    if any(p in REGENERABLE_NAMES for p in parts):
        return True
    name = parts[-1] if parts else rel
    return any(fnmatch.fnmatch(name, g) for g in REGENERABLE_GLOBS)


def _size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    total = 0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return total


# ---- judgement -----------------------------------------------------------------------


@dataclass
class Candidate:
    path: Path
    branch: str
    head: str
    evidence: str
    repo: Path


def _merge_evidence(repo: Path, wt: wtmod.WorktreeInfo) -> str | None:
    if wtmod._is_ancestor_of_default(repo, wt.head_sha):
        return "commits are on the default branch"
    if wtmod._pr_state_for_branch(repo, wt.branch) == "MERGED":
        return f"PR for '{wt.branch}' merged"
    return None


def assess(
    wt: wtmod.WorktreeInfo, repo: Path, *, default_branch: str, cfg: ReapConfig,
    cwds: list[Path] | None, venvs: list[Path] | None, now: float,
) -> tuple[Candidate | None, str]:
    """(candidate, "") when removable, else (None, why not). Cheapest checks first."""
    if wt.locked:
        return None, "locked"
    if not wt.head_sha:
        return None, "bare or unborn entry"
    if wt.branch and wt.branch == default_branch:
        return None, f"checkout of the default branch '{default_branch}'"
    if not wt.path.exists():
        return None, "directory missing (git worktree prune handles this)"
    act = last_activity(wt.path)
    if act is None:
        return None, "last git activity unknown"
    age_h = (now - act) / 3600
    if age_h < cfg.min_age_hours:
        return None, f"active {age_h:.1f}h ago (under {cfg.min_age_hours:g}h)"
    rc, status = _git(["status", "--porcelain"], wt.path)
    if rc != 0:
        return None, "git status failed"
    if status.strip():
        return None, "uncommitted or untracked content"
    evidence = _merge_evidence(repo, wt)
    if evidence is None:
        return None, "commits not on the default branch and no merged PR"
    if cwds is None:
        return None, "cannot tell which processes are inside (fail closed)"
    if any(_inside(c, wt.path) for c in cwds):
        return None, "a process has its working directory inside"
    bound = wtmod.editable_bindings(wt.path, venvs)
    if bound:
        return None, "editable install points here: " + ", ".join(f"{v}:{p}" for v, p in bound)
    return Candidate(wt.path, wt.branch, wt.head_sha, evidence, repo), ""


def _base_checkouts(repo: Path) -> set[Path]:
    rc, out = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo)
    bases: set[Path] = set()
    if rc == 0 and out.strip():
        common = Path(out.strip())
        bases.add(common.resolve())
        bases.add(common.parent.resolve())
    return bases


def plan(
    root: Path, cfg: ReapConfig, *, cwds: list[Path] | None | str = "auto",
    venvs: list[Path] | None = None, now: float | None = None,
) -> tuple[list[Candidate], list[dict[str, str]]]:
    now = time.time() if now is None else now
    if cwds == "auto":
        cwds = process_cwds()
    candidates: list[Candidate] = []
    skipped: list[dict[str, str]] = []
    seen: set[Path] = set()
    for repo in wtmod.discover_repos(Path(root)):
        bases = _base_checkouts(repo)
        default_branch = wtmod._default_branch(repo)
        listed = wtmod.list_worktrees(repo)
        if listed:
            # Run git from the base checkout: the discovered representative may be a
            # linked worktree that this very run removes.
            repo = listed[0].path
        for i, wt in enumerate(listed):
            rp = wt.path.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            if i == 0 or rp in bases:
                skipped.append({"path": str(wt.path), "reason": "base checkout"})
                continue
            cand, why = assess(
                wt, repo, default_branch=default_branch, cfg=cfg, cwds=cwds,
                venvs=venvs, now=now,
            )
            if cand:
                candidates.append(cand)
            else:
                skipped.append({"path": str(wt.path), "reason": why})
    return candidates, skipped


# ---- archive and removal -------------------------------------------------------------


def archive_ignored(
    c: Candidate, state_dir: Path, cfg: ReapConfig, now: float
) -> tuple[Path | None, list[str], str]:
    """Archive ignored non-cache paths. (archive or None, paths archived, error)."""
    entries = ignored_entries(c.path)
    if entries is None:
        return None, [], "could not list ignored files"
    keep = [e for e in entries if not is_regenerable(e)]
    if not keep:
        return None, [], ""
    if sum(_size(c.path / e) for e in keep) > cfg.max_archive_mb * 2**20:
        return None, [], f"ignored state exceeds {cfg.max_archive_mb:g} MB; archive it by hand"
    adir = Path(state_dir) / ARCHIVE_DIR
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    archive = adir / f"{c.path.name}-{stamp}-{c.head[:8]}.tar.gz"
    try:
        adir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "w:gz") as tf:
            for e in keep:
                if not _inside(c.path / e, c.path):
                    raise OSError(f"{e} resolves outside the worktree")
                tf.add(c.path / e, arcname=e)
        with tarfile.open(archive, "r:gz") as tf:  # read it back before trusting it
            names = set(tf.getnames())
        missing = [e for e in keep if e not in names]
        if missing:
            raise OSError(f"archive is missing {missing[0]}")
        entry = {
            "archive": str(archive), "worktree": str(c.path), "branch": c.branch,
            "head": c.head, "paths": keep, "bytes": archive.stat().st_size,
            "at": now, "expires_at": now + cfg.retention_days * 86400,
        }
        with (adir / MANIFEST).open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except (OSError, tarfile.TarError) as exc:
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        return None, [], f"archive failed: {exc}"
    return archive, keep, ""


def _clear(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def reap_one(c: Candidate, state_dir: Path, cfg: ReapConfig, now: float) -> dict[str, Any]:
    archive, archived, err = archive_ignored(c, state_dir, cfg, now)
    if err:
        return {"path": str(c.path), "removed": False, "reason": err}
    try:
        for e in archived:  # only what was archived; caches go with the worktree
            _clear(c.path / e)
    except OSError as exc:
        return {"path": str(c.path), "removed": False, "reason": f"clearing archived paths: {exc}"}
    rc, _ = wtmod._run(["git", "worktree", "remove", str(c.path)], cwd=c.repo, timeout=120)
    if rc != 0 or c.path.exists():
        return {
            "path": str(c.path), "removed": False, "archive": str(archive) if archive else "",
            "reason": "git worktree remove refused (no --force); archive kept",
        }
    return {
        "path": str(c.path), "removed": True, "branch": c.branch, "why": c.evidence,
        "archive": str(archive) if archive else "", "archived_paths": archived,
    }


def expire_archives(state_dir: Path, now: float | None = None) -> list[str]:
    """Delete archives past their retention; rewrite the manifest without them."""
    now = time.time() if now is None else now
    adir = Path(state_dir) / ARCHIVE_DIR
    mf = adir / MANIFEST
    try:
        lines = mf.read_text().splitlines()
    except OSError:
        return []
    kept: list[str] = []
    expired: list[str] = []
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            kept.append(ln)
            continue
        if float(e.get("expires_at", now + 1)) <= now:
            try:
                Path(e["archive"]).unlink(missing_ok=True)
                expired.append(e["archive"])
                continue
            except OSError:
                pass
        kept.append(ln)
    if expired:
        mf.write_text("".join(k + "\n" for k in kept))
    return expired


def reap(
    root: Path, state_dir: Path, cfg: ReapConfig, *, apply: bool = False,
    cwds: list[Path] | None | str = "auto", venvs: list[Path] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    candidates, skipped = plan(root, cfg, cwds=cwds, venvs=venvs, now=now)
    out: dict[str, Any] = {
        "mode": "apply" if apply else "plan",
        "would_remove": [
            {"path": str(c.path), "branch": c.branch, "why": c.evidence} for c in candidates
        ],
        "skipped": skipped, "removed": [], "failed": [], "expired_archives": [],
    }
    if not apply:
        return out
    out["expired_archives"] = expire_archives(state_dir, now)
    for c in candidates[: max(cfg.batch, 0)]:
        r = reap_one(c, state_dir, cfg, now)
        (out["removed"] if r["removed"] else out["failed"]).append(r)
    out["deferred"] = max(len(candidates) - max(cfg.batch, 0), 0)
    return out
