# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where ``program sync`` reads the desired program state from — the seam.

Phase 3 reconciles the program data file against a *source*. The source is
a seam, not a vendor: a clear interface a future capture feeder (GitLab,
GitHub, a wiki — the ADR-162 watcher instances) implements by publishing a
:class:`ProgramData` it assembled, while phase 3 ships the two sources that
make ``sync`` real and testable today:

- :class:`FileSource` — read the desired state from a JSON data file (the
  default source is the node's own ``data.json``, so ``sync`` reconciles a
  hand-edited file against its own last snapshot).
- :class:`NullSource` — a source that has nothing. ``sync`` against it is a
  safe no-op; it is what an unconfigured feeder resolves to, so the verb is
  always callable even before a capture connector exists.

A source carries an ``origin`` — a stable, human-readable label (the shape
ADR-087 calls a ``SourceOrigin``) stamped onto every change-log entry so a
reader can tell a hand edit from a feeder push. The interface is read-only
by construction: a source never writes anything, and ``sync`` is the only
writer of the data file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..model import ProgramData, ProgramError, load_program

if TYPE_CHECKING:
    from ._clients import TrackerClient


# ---------------------------------------------------------------------------
# Connector readiness (prd-program R9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectorReadiness:
    """A source's position on the verification ladder (prd-program R9).

    A source is used only after it climbs the whole ladder — reachable,
    authenticated, per-project read OK. A source that stops short is
    ``unverified``; ``sync`` skips it *loudly* rather than treating missing
    data as "no changes". The rungs are reported individually so a caller
    sees exactly where the climb stopped.
    """

    source: str
    reachable: bool
    authenticated: bool
    project_read: bool
    detail: str = ""

    @property
    def verified(self) -> bool:
        return self.reachable and self.authenticated and self.project_read

    @property
    def failed_rung(self) -> str | None:
        if not self.reachable:
            return "reachable"
        if not self.authenticated:
            return "authenticated"
        if not self.project_read:
            return "project_read"
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "reachable": self.reachable,
            "authenticated": self.authenticated,
            "project_read": self.project_read,
            "verified": self.verified,
            "failed_rung": self.failed_rung,
            "detail": self.detail,
        }


@runtime_checkable
class ProgramSource(Protocol):
    """A read-only source of the desired program state.

    ``load`` returns the program the source currently describes, or ``None``
    when the source has nothing to offer yet (a missing file, an
    unconfigured feeder). It may raise :class:`ProgramError` /
    :class:`ProgramValidationError` when the source *has* content that does
    not parse or validate — ``sync`` turns that into a typed refusal rather
    than writing a broken file.
    """

    @property
    def origin(self) -> str:
        """A stable label for this source, stamped onto every change-log
        entry (e.g. ``file:/path/to/data.json`` or ``null``)."""
        ...

    def load(self) -> ProgramData | None:
        """The program this source describes now, or ``None`` if it has
        nothing."""
        ...

    def verify(self) -> ConnectorReadiness:
        """Climb the connector-readiness ladder (R9). A local source is
        trivially verified; a live feeder probes its connection."""
        ...


def _local_readiness(origin: str) -> ConnectorReadiness:
    """A file/null source needs no network — every rung is trivially met."""
    return ConnectorReadiness(
        source=origin,
        reachable=True,
        authenticated=True,
        project_read=True,
        detail="local source; no connector to verify",
    )


def _finalize_findings(
    base: ProgramData, fresh: list[dict[str, Any]], owned_kinds: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Merge one feeder's freshly-computed findings with the standing set.

    A feeder owns only *its* finding kinds (a tracker feeder owns the
    attribution kinds, the mirror check owns the mirror kinds). It recomputes
    those and replaces them; every other feeder's findings — present when
    ``source=all`` fans out across feeders — is carried forward untouched, so
    one feeder's pass never clears another's.
    """
    result = list(fresh)
    seen = {(f.get("kind"), str(f.get("subject"))) for f in result}
    for f in (base.raw.get("capture") or {}).get("findings") or []:
        if not isinstance(f, dict):
            continue
        if f.get("kind") in owned_kinds:
            continue  # this feeder owns it and has just recomputed it
        if (f.get("kind"), str(f.get("subject"))) in seen:
            continue
        result.append(f)
    return result


class NullSource:
    """A source that has nothing. ``sync`` against it changes nothing.

    This is what an unconfigured capture feeder resolves to: the verb stays
    callable and simply reports that there was nothing to reconcile, rather
    than failing because no connector is wired yet.
    """

    origin = "null"

    def load(self) -> ProgramData | None:
        return None

    def verify(self) -> ConnectorReadiness:
        return _local_readiness(self.origin)


class FileSource:
    """Read the desired program state from a JSON data file.

    A missing file is "nothing to reconcile" (``load`` returns ``None``) — a
    first sync on a node that has no data file yet must not error. A file
    that is present but does not parse or validate raises, so ``sync`` can
    refuse rather than overwrite the current state with a broken one.

    ``require_listed_owners`` mirrors :func:`load_program`'s flag: the
    self-reconcile source loads leniently (``False``) so a file whose only
    defect is a drift finding — an owner not yet in ``people`` — can be
    reconciled and have that drift reported, exactly as the ``drift`` read
    tolerates it. An explicit upstream source loads strictly.
    """

    def __init__(self, path: str | Path, *, require_listed_owners: bool = True) -> None:
        self.path = Path(path)
        self._require_listed_owners = require_listed_owners

    @property
    def origin(self) -> str:
        return f"file:{self.path}"

    def load(self) -> ProgramData | None:
        if not self.path.exists():
            return None
        return load_program(self.path, require_listed_owners=self._require_listed_owners)

    def verify(self) -> ConnectorReadiness:
        return _local_readiness(self.origin)


# ---------------------------------------------------------------------------
# Live capture feeders — ADR-162 watcher instances behind the seam
# ---------------------------------------------------------------------------


class _LiveTrackerSource:
    """Shared base for the GitLab/GitHub feeders (ADR-162 instances).

    An instance declares only the vendor-specific bits — the ``system``
    label, how to build its read-only client, the tracker config it reads —
    and inherits the rest from the primitive and the capture judgement:

    - the **watcher primitive** (``axiom.infra.watcher``) owns the cursor
      (``updated_after``), the debounce, and the content-identity dedup;
    - :mod:`._capture` owns attribution, the proxy-assignee rule, and the
      findings;
    - this base wires them into a :class:`ProgramSource`: ``load`` overlays
      the live tracker state onto the node's current data file (the
      authoritative structure) and records the findings in a ``capture``
      block, so ``sync`` diffs the result exactly as it diffs a file.

    The feeder is **read-only**: it ingests, attributes, and detects. The
    proxy assignee is *recorded* as the intended assignee for a later posting
    phase; nothing here writes to the tracker.
    """

    system = "tracker"

    def __init__(
        self,
        data_path: str | Path,
        *,
        client: TrackerClient | None = None,
        state_dir: str | Path | None = None,
        credential_name: str | None = None,
        debounce_seconds: int = 0,
        link_checker: Any | None = None,
    ) -> None:
        self.data_path = Path(data_path)
        self._injected_client = client
        self._credential_name = credential_name
        self._debounce = debounce_seconds
        self._injected_link_checker = link_checker
        if state_dir is not None:
            self._state_dir = Path(state_dir)
        else:
            # <state_dir>/program/data.json → <state_dir>
            self._state_dir = self.data_path.parent.parent
        self._client_cached: TrackerClient | None = client
        self._client_error = ""
        self._base: ProgramData | None = None

    # -- base program + tracker config -------------------------------------

    def _load_base(self) -> ProgramData | None:
        if not self.data_path.exists():
            return None
        # Lenient: overlay onto whatever structure is there. sync re-validates
        # strictly on the way out, so a base whose only defect is drift is
        # still reconcilable, as the self-reconcile source is.
        return load_program(self.data_path, require_listed_owners=False)

    def _tracker(self) -> dict[str, Any]:
        base = self._base or self._load_base()
        return base.tracker() if base is not None else {}

    @property
    def origin(self) -> str:
        return f"{self.system}:{self._origin_suffix()}"

    def _origin_suffix(self) -> str:  # overridden per vendor
        return "tracker"

    # -- client (injected, else built from the vault credential) -----------

    def _build_client(self) -> TrackerClient | None:  # overridden per vendor
        raise NotImplementedError

    def _client(self) -> TrackerClient | None:
        if self._client_cached is not None:
            return self._client_cached
        if self._injected_client is not None:
            self._client_cached = self._injected_client
            return self._client_cached
        try:
            self._client_cached = self._build_client()
        except Exception as exc:  # noqa: BLE001 — a build failure is "unverified"
            self._client_error = str(exc)
            self._client_cached = None
        return self._client_cached

    # -- the connector-readiness ladder (R9) -------------------------------

    def verify(self) -> ConnectorReadiness:
        client = self._client()
        if client is None:
            return ConnectorReadiness(
                self.origin,
                reachable=False,
                authenticated=False,
                project_read=False,
                detail=self._client_error or "no client (credential unavailable)",
            )
        if not client.ping():
            return ConnectorReadiness(self.origin, False, False, False, "host unreachable")
        who = client.whoami()
        if not who:
            return ConnectorReadiness(self.origin, True, False, False, "authentication failed")
        if not client.project_readable():
            return ConnectorReadiness(
                self.origin, True, True, False, f"authenticated as {who}; project not readable"
            )
        return ConnectorReadiness(self.origin, True, True, True, f"authenticated as {who}")

    # -- the read -----------------------------------------------------------

    def _mirror_findings(self, base: ProgramData) -> list[dict[str, Any]]:
        """Overridden by the mirror-check home (GitHub)."""
        return []

    def load(self) -> ProgramData | None:
        from axiom.infra.watcher import FileWatcherStore, Watcher, WatchItem

        from . import _capture as cap

        base = self._load_base()
        if base is None:
            return None
        self._base = base

        client = self._client()
        if client is None:
            # Verified-first is sync's contract; defend anyway so a direct
            # caller never gets a silent empty overlay.
            raise ProgramError(f"{self.origin}: no client ({self._client_error})")

        store = FileWatcherStore(self._state_dir / "program" / "watchers")
        state = store.load(self.origin)

        def fetch(since: str | None) -> list[WatchItem]:
            items: list[WatchItem] = []
            for issue in client.issues(since):
                items.append(
                    WatchItem(
                        identity=f"issue:{issue.ref}",
                        kind="tracker.issue",
                        payload={"obj": issue, "updated_at": issue.updated_at},
                        landed=issue.state in ("closed", "merged"),
                    )
                )
            for mr in client.merge_requests(since):
                items.append(
                    WatchItem(
                        identity=f"mr:{mr.ref}",
                        kind="scm.merge_request",
                        payload={"obj": mr, "updated_at": mr.updated_at},
                        landed=mr.landed,
                    )
                )
            return items

        watcher = Watcher(name=self.origin, fetch=fetch, debounce_seconds=self._debounce)
        result = watcher.poll(state)
        if result.polled:
            store.save(self.origin, result.state)

        issues = [it.payload["obj"] for it in result.items if it.identity.startswith("issue:")]
        mrs = [it.payload["obj"] for it in result.items if it.identity.startswith("mr:")]

        captured = cap.attribute_and_overlay(base, issues, mrs, system=self.system)
        overlaid = captured.program

        fresh = list(captured.findings)
        fresh.extend(self._carry_forward_untracked(base, overlaid, captured.findings))
        mirror = self._mirror_findings(overlaid)
        fresh.extend(mirror)

        # Crosslink health (R11): confirm each bound issue and each declared
        # endpoint still resolves, read-only, honouring connector readiness —
        # an unreachable checker yields `unverified`, never 'healthy'.
        health = self._check_links(overlaid)
        fresh.extend(health.findings)

        owned = (
            cap.TRACKER_FINDING_KINDS
            + cap.LINK_FINDING_KINDS
            + (cap.MIRROR_FINDING_KINDS if mirror or self._owns_mirror() else ())
        )
        findings = _finalize_findings(base, fresh, owned)

        captured.raw["capture"] = {
            "source": self.origin,
            "system": self.system,
            "verified": True,
            "ladder": self.verify().as_dict(),
            "link_health": health.as_block(),
            "findings": findings,
        }
        return ProgramData(raw=captured.raw)

    def _link_checker(self) -> Any:
        """The crosslink checker: the injected one (tests), else one built on
        the (verified, read-only) tracker client."""
        if self._injected_link_checker is not None:
            return self._injected_link_checker
        from ._clients import ClientLinkChecker

        return ClientLinkChecker(self._client())

    def _check_links(self, overlaid: ProgramData) -> Any:
        from . import _capture as cap
        from ._source import CANONICAL_ENDPOINT

        canonical = overlaid.endpoints().get(CANONICAL_ENDPOINT)
        return cap.check_links(overlaid, self._link_checker(), canonical=canonical)

    def _owns_mirror(self) -> bool:
        """Whether this feeder's pass recomputes the mirror findings (the
        GitHub source does; the GitLab source never does)."""
        return False

    @staticmethod
    def _carry_forward_untracked(
        base: ProgramData, overlaid: ProgramData, fresh: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Keep a prior ``untracked_issue`` finding standing across a delta.

        A cursor-delta read only re-sees changed issues, so an orphan seen in
        an earlier cycle would otherwise clear the next quiet cycle. Carry it
        forward unless the issue is now bound to a schedule item (self-clear)
        or it is already in this cycle's fresh set."""
        bound = {
            str(i.get("issue"))
            for i in overlaid.schedule
            if isinstance(i, dict) and i.get("issue") is not None
        }
        fresh_subjects = {(f["kind"], str(f["subject"])) for f in fresh}
        prior = (base.raw.get("capture") or {}).get("findings") or []
        carried: list[dict[str, Any]] = []
        for f in prior:
            if f.get("kind") != "untracked_issue":
                continue
            subject = str(f.get("subject"))
            if subject in bound:
                continue  # now tracked → self-cleared
            if ("untracked_issue", subject) in fresh_subjects:
                continue  # already in this cycle's fresh findings
            carried.append(f)
        return carried


class GitLabSource(_LiveTrackerSource):
    """The program tracker on GitLab, read cursor-first (updated_after).

    Reads issues (assignee, state, milestone/due, labels, title) and the
    merge requests linked to them, maps each issue to the schedule item whose
    ``issue`` binding matches, and overlays the live facts without clobbering
    the human-committed fields. Credential from the vault (named in
    ``program.tracker.credential``); read-only.
    """

    system = "gitlab"

    def _origin_suffix(self) -> str:
        tracker = self._tracker()
        return f"{tracker.get('host', 'unknown')}/{tracker.get('project_id', '?')}"

    def _build_client(self) -> TrackerClient | None:
        from ._clients import GitLabClient, resolve_vault_token

        tracker = self._tracker()
        host = tracker.get("host")
        project_id = tracker.get("project_id")
        name = self._credential_name or tracker.get("credential")
        if not host or project_id is None:
            raise ProgramError("program.tracker must name host and project_id for a gitlab source")
        token = resolve_vault_token(name or "", self._state_dir)
        return GitLabClient(host=host, project_id=project_id, token=token)


class GitHubSource(_LiveTrackerSource):
    """The GitHub feeder — same tracker shape as GitLab, plus the mirror check.

    When the program's tracker is on GitHub it overlays issues/PRs as the
    GitLab source does. It is additionally the **mirror-check home**: it reads
    the declared mirror pairs (``program.mirrors``), confirms both sides agree
    by commit identity (a commit seen through both origin and mirror collapses
    on its SHA — the ADR-162 rule, no double-count), and emits ``mirror_gap``
    / ``mirror_stale`` / ``mirror_missing`` findings. Mirror state a side
    cannot confirm is reported unverified, never rounded up to synced.
    """

    system = "github"

    def __init__(
        self,
        data_path: str | Path,
        *,
        commit_reader: Callable[[str, str], set[str] | None] | None = None,
        recent_reader: Callable[[Any], bool | None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(data_path, **kwargs)
        self._commit_reader = commit_reader
        self._recent_reader = recent_reader

    def _origin_suffix(self) -> str:
        tracker = self._tracker()
        repo = tracker.get("repo")
        return repo if isinstance(repo, str) and repo else "mirror"

    def _owns_mirror(self) -> bool:
        return True

    def _build_client(self) -> TrackerClient | None:
        from ._clients import GitHubCliClient

        tracker = self._tracker()
        repo = tracker.get("repo") or ""
        return GitHubCliClient(repo=repo)

    def load(self) -> ProgramData | None:
        # A github-only mirror checker (no github tracker) still has a job to
        # do, so it tolerates a tracker on another host: when there is no
        # github tracker, it overlays nothing and only runs the mirror check.
        tracker = self._tracker()
        if tracker.get("kind") != "github":
            return self._mirror_only_load()
        return super().load()

    def _mirror_only_load(self) -> ProgramData | None:
        import copy

        from . import _capture as cap

        base = self._load_base()
        if base is None:
            return None
        self._base = base
        fresh = self._mirror_findings(base)
        findings = _finalize_findings(base, fresh, cap.MIRROR_FINDING_KINDS)

        raw = copy.deepcopy(base.raw)
        raw["capture"] = {
            "source": self.origin,
            "system": self.system,
            "verified": True,
            "ladder": None,
            "findings": findings,
            "mode": "mirror-only",
        }
        return ProgramData(raw=raw)

    def _mirror_findings(self, base: ProgramData) -> list[dict[str, Any]]:
        from . import _capture as cap

        pairs = cap.declared_mirror_pairs(base)
        tracker = base.tracker()
        tracker_repo = None
        if isinstance(tracker.get("host"), str) and isinstance(tracker.get("repo"), str):
            tracker_repo = (tracker["host"], tracker["repo"])
        expect = bool(base.program.get("mirror_expected"))
        if not pairs and not (expect and tracker_repo):
            return []

        reader = self._commit_reader or self._default_commit_reader()
        commits: dict[tuple[str, str], set[str] | None] = {}
        for pair in pairs:
            for host, repo in (
                (pair.origin_host, pair.origin_repo),
                (pair.mirror_host, pair.mirror_repo),
            ):
                if (host, repo) not in commits:
                    commits[(host, repo)] = reader(host, repo)

        recent: dict[str, bool | None] = {}
        if self._recent_reader is not None:
            for pair in pairs:
                recent[pair.subject] = self._recent_reader(pair)

        return cap.check_mirror(
            pairs,
            commits,
            recent=recent,
            tracker_repo=tracker_repo,
            expect_mirror=expect,
        )

    def _default_commit_reader(self) -> Callable[[str, str], set[str] | None]:
        """Best-effort reader: use the GitHub client for any host; a host it
        cannot read returns ``None`` (unverified, never 'synced'). Reading the
        opposite host of a cross-vendor pair needs that host's own credential,
        which is deployment wiring beyond this phase — so a deployment injects
        a reader, or that side is honestly reported unverified."""
        client = self._client()

        def reader(host: str, repo: str) -> set[str] | None:
            if client is None:
                return None
            try:
                return client.commits(host, repo)
            except Exception:  # noqa: BLE001
                return None

        return reader


# ---------------------------------------------------------------------------
# Source selection (sync: file | gitlab | github | all)
# ---------------------------------------------------------------------------

#: The source kinds ``program sync`` can select.
SOURCE_KINDS = ("file", "gitlab", "github", "all")


def build_source(
    kind: str,
    data_path: str | Path,
    *,
    state_dir: str | Path | None = None,
    require_listed_owners: bool = False,
) -> ProgramSource:
    """Build one live or file source by kind.

    ``file`` is the self-reconcile ``FileSource``; ``gitlab`` / ``github`` are
    the live feeders. ``all`` is handled by the caller (it fans out to every
    configured live source); this builds a single one.
    """
    if kind == "file":
        return FileSource(data_path, require_listed_owners=require_listed_owners)
    if kind == "gitlab":
        return GitLabSource(data_path, state_dir=state_dir)
    if kind == "github":
        return GitHubSource(data_path, state_dir=state_dir)
    raise ValueError(f"unknown source kind {kind!r}; one of {', '.join(SOURCE_KINDS)}")


__all__ = [
    "ProgramSource",
    "NullSource",
    "FileSource",
    "GitLabSource",
    "GitHubSource",
    "ConnectorReadiness",
    "SOURCE_KINDS",
    "build_source",
    "ProgramError",
]
