# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which repositories are origins, and which are copies of another.

A set of repositories often holds the same work twice: a mirror kept in sync
from another forge, a fork, an archived predecessor. Anything that counts work
per repository (a program rollup, a CI spend report) double-counts unless it
can tell them apart. This module classifies each repository as one of
:data:`CLASSES` from evidence only:

1. **Declared pairs** (``{"origin": {host, repo}, "mirror": {host, repo}}``)
   always win: a deployment's own statement beats anything inferred.
2. **Forge metadata**: ``archived``; GitHub's ``mirror_url`` (a pull mirror),
   ``fork`` with ``parent``/``source``; GitLab's ``mirror`` flag with
   ``import_url``, ``forked_from_project``, and a ``push_mirrors`` list on the
   origin naming its targets.
3. **Shared commits**: when one repository's head commit appears in another's
   history and the other has moved past it, the first is a lagging copy of the
   second. Identical histories say the two are one lineage but not which is
   the origin, so both stay ``unverified`` and say why.

Nothing is guessed. A repository with no metadata and no commit evidence is
``unverified``; an unreadable history is no evidence. History reads are
bounded (``max_history_reads``) and skip anything metadata already settled.

Pure: no I/O. Callers hand in the metadata they already fetched and, if they
want commit evidence, a ``history(host, repo) -> [sha, …] | None`` reader
(newest first).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any
from urllib.parse import urlsplit

#: The classifications, in the order a report lists them.
CLASSES = ("origin", "mirror", "fork", "archived", "unverified")

#: Metadata keys whose presence means "the forge described this repository",
#: so their absence of a fork/mirror mark is itself evidence of an origin.
_DESCRIPTIVE_KEYS = ("fork", "mirror", "mirror_url", "forked_from_project", "archived")

HistoryReader = Callable[[str, str], Sequence[str] | None]


def _name(entry: dict[str, Any]) -> str | None:
    for key in ("full_name", "repo", "path_with_namespace"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _ref_name(value: Any) -> str | None:
    """``owner/name`` from a string or a forge object naming a repository."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        return _name(value)
    return None


def _url_ref(url: Any) -> tuple[str, str] | None:
    """``(host, owner/name)`` from a clone URL, or ``None``."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    path = (parts.path or "").strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not parts.hostname or "/" not in path:
        return None
    return parts.hostname.lower(), path


def _key(host: str, repo: str) -> tuple[str, str]:
    return host.lower(), repo.lower()


class _Repo:
    def __init__(self, host: str, repo: str, meta: dict[str, Any] | None) -> None:
        self.host = host
        self.repo = repo
        self.meta = meta or {}
        self.classification = "unverified"
        self.of: str | None = None
        self.evidence: list[str] = []
        self.same_lineage: list[str] = []
        self.settled = False  # declared or decided by metadata; commit evidence does not reopen it

    def set(
        self, classification: str, why: str, *, of: str | None = None, settled: bool = True
    ) -> None:
        self.classification = classification
        self.of = of
        self.evidence.append(why)
        self.settled = settled

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "host": self.host,
            "classification": self.classification,
            "of": self.of,
            "evidence": list(self.evidence),
            "same_lineage": list(self.same_lineage),
        }


def _normalize(entries: Iterable[Any], default_host: str) -> list[_Repo]:
    out: list[_Repo] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if isinstance(entry, str) and "/" in entry:
            repo = _Repo(default_host, entry, None)
        elif isinstance(entry, dict) and _name(entry):
            repo = _Repo(str(entry.get("host") or default_host), str(_name(entry)), entry)
        else:
            continue
        key = _key(repo.host, repo.repo)
        if key in seen:
            continue
        seen.add(key)
        out.append(repo)
    return out


def _apply_declared(repos: dict[tuple[str, str], _Repo], declared: Iterable[Any]) -> None:
    for pair in declared:
        if not isinstance(pair, dict):
            continue
        origin, mirror = pair.get("origin") or {}, pair.get("mirror") or {}
        if not (isinstance(origin, dict) and isinstance(mirror, dict)):
            continue
        o_repo, m_repo = origin.get("repo"), mirror.get("repo")
        if not (isinstance(o_repo, str) and isinstance(m_repo, str)):
            continue
        o_host, m_host = str(origin.get("host") or ""), str(mirror.get("host") or "")
        target = repos.get(_key(m_host, m_repo))
        if target is not None:
            target.set(
                "mirror", f"declared mirror of {o_host}/{o_repo} in program.mirrors", of=o_repo
            )
        source = repos.get(_key(o_host, o_repo))
        if source is not None and not source.settled:
            source.set("origin", f"declared origin of {m_host}/{m_repo} in program.mirrors")


def _apply_metadata(repos: dict[tuple[str, str], _Repo]) -> None:
    # Push mirrors are declared on the origin and name their targets.
    for src in repos.values():
        for url in src.meta.get("push_mirrors") or []:
            ref = _url_ref(url if isinstance(url, str) else (url or {}).get("url"))
            target = repos.get(_key(*ref)) if ref else None
            if target is not None and not target.settled:
                target.set("mirror", f"{src.host}/{src.repo} pushes a mirror to it", of=src.repo)

    for repo in repos.values():
        if repo.settled:
            continue
        meta = repo.meta
        if meta.get("archived"):
            repo.set("archived", "the forge marks it archived")
            continue
        mirror_url = meta.get("mirror_url")
        if isinstance(mirror_url, str) and mirror_url:
            ref = _url_ref(mirror_url)
            repo.set(
                "mirror",
                "the forge reports a mirror source (mirror_url)",
                of="/".join(ref) if ref else mirror_url,
            )
            continue
        if meta.get("mirror") is True:
            ref = _url_ref(meta.get("import_url"))
            repo.set(
                "mirror", "the forge marks it a pull mirror", of="/".join(ref) if ref else None
            )
            continue
        forked_from = _ref_name(meta.get("forked_from_project"))
        if meta.get("fork") is True or forked_from:
            parent = forked_from or _ref_name(meta.get("parent")) or _ref_name(meta.get("source"))
            repo.set(
                "fork", "the forge marks it a fork" + (f" of {parent}" if parent else ""), of=parent
            )
            continue
        if any(key in meta for key in _DESCRIPTIVE_KEYS):
            repo.set("origin", "the forge marks it neither a fork nor a mirror", settled=False)


def _apply_shared_commits(
    candidates: list[_Repo], history: HistoryReader, max_reads: int
) -> tuple[int, bool]:
    histories: dict[int, list[str]] = {}
    reads = 0
    truncated = False
    for index, repo in enumerate(candidates):
        if reads >= max_reads:
            truncated = True
            break
        reads += 1
        try:
            got = history(repo.host, repo.repo)
        except Exception:  # noqa: BLE001 — an unreadable history is no evidence
            got = None
        if got:
            histories[index] = [str(sha) for sha in got if sha]

    for i, a in enumerate(candidates):
        ha = histories.get(i)
        if not ha:
            continue
        for j, b in enumerate(candidates):
            hb = histories.get(j)
            if i == j or not hb:
                continue
            if ha[0] == hb[0]:
                if b.repo not in a.same_lineage:
                    a.same_lineage.append(b.repo)
            elif ha[0] in hb[1:] and a.classification != "mirror":
                a.set(
                    "mirror",
                    f"head {ha[0][:12]} is in {b.repo}'s history and {b.repo} has moved past it",
                    of=b.repo,
                    settled=False,
                )
    for repo in candidates:
        if repo.same_lineage and repo.classification != "mirror":
            repo.set(
                "unverified",
                "same history as "
                + ", ".join(repo.same_lineage)
                + "; which one is the origin cannot be told from commits (declare the pair)",
                settled=False,
            )
    return reads, truncated


def classify(
    repos: Iterable[Any],
    *,
    declared: Iterable[Any] = (),
    history: HistoryReader | None = None,
    default_host: str = "github.com",
    max_history_reads: int = 30,
) -> dict[str, Any]:
    """Classify each repository; see the module docstring for the evidence order.

    ``repos`` holds ``owner/name`` strings (no metadata) or forge objects
    (``full_name``/``path_with_namespace`` plus whatever metadata the caller
    already read). Returns ``{repos: [...], counts, history_reads, truncated}``,
    each repo ``{repo, host, classification, of, evidence, same_lineage}``.
    """
    ordered = _normalize(repos, default_host)
    index = {_key(r.host, r.repo): r for r in ordered}
    _apply_declared(index, declared)
    _apply_metadata(index)

    reads, truncated = 0, False
    candidates = [r for r in ordered if not r.settled]
    if history is not None and len(candidates) >= 2:
        reads, truncated = _apply_shared_commits(
            candidates, history, max(0, int(max_history_reads))
        )

    for repo in ordered:
        if not repo.evidence:
            repo.evidence.append("no metadata and no commit evidence")

    counts = {name: 0 for name in CLASSES}
    for repo in ordered:
        counts[repo.classification] += 1
    return {
        "repos": [r.as_dict() for r in ordered],
        "counts": counts,
        "history_reads": reads,
        "truncated": truncated,
    }


__all__ = ["CLASSES", "HistoryReader", "classify"]
