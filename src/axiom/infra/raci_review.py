# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shadow risk-tier review for pull-request landing (ADR-045 v0.3).

The tier is computed, written to the graduation shadow log and the
action ledger, and reported through HERALD. It does not change what
Rivet merges or holds. A model's risk opinion is recorded and never
moves the tier (ADR-152).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from axiom.infra.raci import (
    SHIPPED_PRESET,
    canonical_action,
    matrix_tier,
    operating_trust,
    toward_approve,
)
from axiom.policy.agent_action_guard import AGENT_ACTION_DEFAULT_MAX_PER_TICK

_log = logging.getLogger(__name__)

DEFAULT_APPROVERS = ("@benjaminbooth", "@luker-robinson2")
DEFAULT_APPROVAL_COUNT = 1
_VOLUME_FACTOR = 3

_DEFAULT_PATTERNS = (
    "auth/**",
    "crypto/**",
    "policy/**",
    "infra/raci.py",
    "infra/identity/**",
    "**/*secret*",
    "**/*credential*",
    ".github/workflows/**",
    "pyproject.toml",
    "uv.lock",
)

_URGENT_CATEGORIES = frozenset({"security", "billing", "pricing", "prod"})
_LOCK_NAMES = frozenset({
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
    "cargo.lock",
    "gemfile.lock",
    "composer.lock",
    "pipfile.lock",
    "packages.lock.json",
    "go.sum",
    "npm-shrinkwrap.json",
    "flake.lock",
})
_VENDOR_SEGMENTS = frozenset({"vendor", "third_party", "node_modules", "vendored"})
_GENERATED_SEGMENTS = frozenset({"generated", "__generated__", "gen"})
_SNAPSHOT_SEGMENTS = frozenset({"__snapshots__", "snapshots"})

_DESTRUCTIVE_SQL = re.compile(
    r"\b(?:DROP\s+(?:TABLE|SCHEMA|DATABASE|INDEX)|TRUNCATE\s+(?:TABLE\s+)?"
    r"|DELETE\s+FROM|ALTER\s+TABLE\s+\S+\s+DROP)\b",
    re.IGNORECASE,
)
_IRREVERSIBLE_SQL = re.compile(
    r"\b(?:DROP\s+(?:DATABASE|SCHEMA)|TRUNCATE\s+(?:TABLE\s+)?|DROP\s+TABLE)\b",
    re.IGNORECASE,
)
_DELETE_FROM = re.compile(r"\bDELETE\s+FROM\s+.+", re.IGNORECASE)
_PROD_HINT = re.compile(r"(^|[/._\s])(prod|production)([/._\s]|$)", re.IGNORECASE)
_STRIPE_PRICE = re.compile(r"\bprice_[A-Za-z0-9]{10,}\b")
_STRIPE_LIVE_SECRET = re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{10,}\b")
_FORCE_PUSH = re.compile(r"git\s+push\b[^\n]*(?:\s-f\b|\s--force(?:-with-lease)?\b)")
_REWRITE = re.compile(
    r"git\s+(?:filter-branch|filter-repo)\b|git\s+rebase\b[^\n]*\b(?:main|master)\b"
)
_DEFAULT_BRANCH = re.compile(r"\b(?:main|master)\b|default branch", re.IGNORECASE)
_DEPLOY_KEYS = (
    "required_approving_review_count",
    "required_reviewers",
    "prevent_self_review",
    "wait_timer",
    "wait-timer",
    "protection_rules",
    "deployment_branch_policy",
)
_CHECK_KEYS = (
    "required_status_checks",
    "required_conversation_resolution",
    "enforce_admins",
)

_PRESET_EXPLANATION = """\
Rivet's review preset is autonomous (trust 4-5).
Routine reversible pull-request landings are act-then-notify: the tier
is computed, a digest is reported, and the tier carries an undo window.
Shadow mode is on. The decision is written to the graduation shadow log
and the action ledger. What Rivet merges or holds stays as it is today.

The preset sits behind the autonomy dial (autonomy.enabled), which
ships off. Heartbeat agents stay quiet until that dial is turned on.
"""


@dataclass(frozen=True)
class FileChange:
    """One path in a change, with the lines that count toward blast radius."""

    path: str
    additions: int = 0
    deletions: int = 0
    rename: bool = False
    added_text: str = ""


@dataclass(frozen=True)
class CategoryRoster:
    roster: tuple[str, ...]
    count: int = DEFAULT_APPROVAL_COUNT


@dataclass(frozen=True)
class BlastFloor:
    """Configured blast-radius floor. Learning may rise above it."""

    lines_medium: int = 800
    lines_high: int = 2000
    files_medium: int = 25
    files_high: int = 50
    learn: bool = True


@dataclass(frozen=True)
class ReviewConfig:
    sensitive_patterns: tuple[str, ...] = _DEFAULT_PATTERNS
    destructive_sql: bool = True
    live_stripe_price_ids: bool = True
    secrets: bool = True
    blast: BlastFloor = field(default_factory=BlastFloor)
    approvers: tuple[str, ...] = DEFAULT_APPROVERS
    approval_count: int = DEFAULT_APPROVAL_COUNT
    category_approvers: tuple[tuple[str, CategoryRoster], ...] = ()


@dataclass(frozen=True)
class ReviewRequest:
    """Facts the tier is computed from. A model opinion is advisory only."""

    action: str = "merge"
    paths: tuple[str, ...] = ()
    diff: str = ""
    additions: int = 0
    deletions: int = 0
    files: tuple[FileChange, ...] = ()
    preset: str = SHIPPED_PRESET
    explicit_trust: int | None = None
    autonomy_enabled: bool = False
    force_push_default: bool = False
    history_rewrite_default: bool = False
    model_risk: str | None = None
    author_model: str = ""
    review_models: tuple[str, ...] = ()
    separate_reviewer: bool = True
    seen_scopes: tuple[str, ...] = ()
    scope: str = ""
    repo: str = ""
    branch: str = ""
    recent_batch_sizes: tuple[int, ...] = ()
    invocations_in_window: int = 0
    batch_size: int | None = None
    human_categories: tuple[str, ...] = ()
    proposed_thresholds: Mapping[str, int] | None = None
    refit_approved: bool = False
    config: ReviewConfig | None = None
    candidate: str = ""
    removed_required_checks: tuple[str, ...] = ()
    loosened_deploy_gates: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskDecision:
    """A shadow tier. ``live_effect`` stays ``unchanged``."""

    tier: str
    action: str
    reasons: tuple[str, ...]
    priority: str
    event: str
    summary: str
    blast_band: str
    counted_lines: int
    counted_files: int
    excluded: tuple[str, ...]
    sensitive_paths: tuple[str, ...]
    content_signals: tuple[str, ...]
    floor: tuple[str, ...]
    gate: tuple[str, ...]
    approvers: tuple[str, ...]
    approvals_required: int
    preset: str
    trust: int
    autonomy_enabled: bool
    model_advisory: str | None
    dual_review_ok: bool | None
    reviewers: tuple[str, ...]
    volume_brake: bool
    first_scope: bool
    candidate: str
    recipient: str = "@operator"
    refit_notes: tuple[str, ...] = ()
    shadow: bool = True
    live_effect: str = "unchanged"

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "action": self.action,
            "reasons": list(self.reasons),
            "priority": self.priority,
            "event": self.event,
            "summary": self.summary,
            "blast_band": self.blast_band,
            "counted_lines": self.counted_lines,
            "counted_files": self.counted_files,
            "excluded": list(self.excluded),
            "sensitive_paths": list(self.sensitive_paths),
            "content_signals": list(self.content_signals),
            "floor": list(self.floor),
            "gate": list(self.gate),
            "approvers": list(self.approvers),
            "approvals_required": self.approvals_required,
            "preset": self.preset,
            "trust": self.trust,
            "autonomy_enabled": self.autonomy_enabled,
            "model_advisory": self.model_advisory,
            "dual_review_ok": self.dual_review_ok,
            "reviewers": list(self.reviewers),
            "volume_brake": self.volume_brake,
            "first_scope": self.first_scope,
            "candidate": self.candidate,
            "recipient": self.recipient,
            "refit_notes": list(self.refit_notes),
            "shadow": self.shadow,
            "live_effect": self.live_effect,
        }


def scope_key(repo: str, branch: str, paths: Sequence[str]) -> str:
    """Envelope key: repo, branch-name shape, and top-level path prefixes."""
    prefixes = sorted({(path.split("/", 1)[0] or path) for path in paths if path})
    shape = branch.split("/", 1)[0] if branch else ""
    return "|".join((repo, shape, ",".join(prefixes)))


def refit_thresholds(
    floor: BlastFloor,
    proposed: Mapping[str, int],
    *,
    approved: bool = False,
) -> tuple[BlastFloor, tuple[str, ...]]:
    """Apply a learned refit. Values may rise. A drop needs ``approved``."""
    if not floor.learn:
        return floor, ("learn is off; thresholds stay at the configured floor",)
    notes: list[str] = []
    values: dict[str, int] = {}
    for name in ("lines_medium", "lines_high", "files_medium", "files_high"):
        current = int(getattr(floor, name))
        if name not in proposed:
            values[name] = current
            continue
        try:
            wanted = int(proposed[name])
        except (TypeError, ValueError):
            values[name] = current
            notes.append(f"{name} proposal ignored")
            continue
        if wanted >= current:
            values[name] = wanted
            if wanted != current:
                notes.append(f"{name} refit {current} -> {wanted}")
        elif approved:
            values[name] = wanted
            notes.append(f"{name} lowered {current} -> {wanted} with approval")
        else:
            values[name] = current
            notes.append(f"{name} held at floor {current}")
    if values["lines_high"] < values["lines_medium"]:
        values["lines_high"] = values["lines_medium"]
        notes.append("lines_high raised to the medium line count")
    if values["files_high"] < values["files_medium"]:
        values["files_high"] = values["files_medium"]
        notes.append("files_high raised to the medium file count")
    return BlastFloor(learn=True, **values), tuple(notes)


def review_config_from_mapping(data: Mapping) -> ReviewConfig:
    """Build a config from a parsed ``.axiom/raci.toml`` document."""
    raci = data.get("raci") if isinstance(data.get("raci"), Mapping) else data
    if not isinstance(raci, Mapping):
        return ReviewConfig()
    paths = raci.get("sensitive_paths") if isinstance(raci.get("sensitive_paths"), Mapping) else {}
    patterns = paths.get("patterns") if isinstance(paths, Mapping) else None
    content = raci.get("sensitive_content") if isinstance(raci.get("sensitive_content"), Mapping) else {}
    blast_raw = raci.get("blast_radius") if isinstance(raci.get("blast_radius"), Mapping) else {}
    approver_raw = raci.get("approvers") if isinstance(raci.get("approvers"), Mapping) else {}
    blast = _blast_from(blast_raw if isinstance(blast_raw, Mapping) else {})
    roster, count, categories = _approvers_from(
        approver_raw if isinstance(approver_raw, Mapping) else {}
    )
    sensitive = _DEFAULT_PATTERNS
    if isinstance(patterns, list) and patterns:
        sensitive = tuple(str(item) for item in patterns)
    return ReviewConfig(
        sensitive_patterns=sensitive,
        destructive_sql=bool(content.get("destructive_sql", True)) if isinstance(content, Mapping) else True,
        live_stripe_price_ids=(
            bool(content.get("live_stripe_price_ids", True)) if isinstance(content, Mapping) else True
        ),
        secrets=bool(content.get("secrets", True)) if isinstance(content, Mapping) else True,
        blast=blast,
        approvers=roster,
        approval_count=count,
        category_approvers=categories,
    )


def load_review_config(path: Path | None = None, *, start: Path | None = None) -> ReviewConfig:
    """Load ``.axiom/raci.toml``. A missing file is the shipped default."""
    found = path if path is not None else discover_raci_toml(start)
    if found is None or not Path(found).is_file():
        return ReviewConfig()
    text = Path(found).read_text(encoding="utf-8")
    return review_config_from_mapping(tomllib.loads(text))


def discover_raci_toml(start: Path | None = None) -> Path | None:
    """The nearest ``.axiom/raci.toml`` at or above ``start``, stopping at a repo root."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / ".axiom" / "raci.toml"
        if candidate.is_file():
            return candidate
        if (directory / ".git").exists():
            return None
    return None


def evaluate(request: ReviewRequest) -> RiskDecision:
    """Compute the shadow tier. The result never authorizes a landing."""
    config = request.config or ReviewConfig()
    preset = request.preset if request.preset in {"strict", "balanced", "autonomous"} else SHIPPED_PRESET
    trust = operating_trust(preset, request.explicit_trust)
    action = canonical_action(request.action)
    tier = matrix_tier(action, trust)
    reasons = [f"matrix {action} at trust {trust} is {tier}"]

    thresholds, refit_notes = refit_thresholds(
        config.blast,
        request.proposed_thresholds or {},
        approved=request.refit_approved,
    )
    reasons.extend(refit_notes)

    changes = _changes(request)
    lines, files, excluded = _measure(changes, request.additions, request.deletions)
    band = _band(lines, files, thresholds)
    if band != "low":
        reasons.append(f"blast radius {band}: {lines} lines, {files} files")

    sensitive = _matching_paths(
        [change.path for change in changes] or list(request.paths),
        config.sensitive_patterns,
    )
    if sensitive:
        tier = toward_approve(tier)
        reasons.append("sensitive path")

    signals, floor, _prod_sql = _content(request, changes, config)
    if signals:
        reasons.append("content signal: " + ", ".join(signals))
    # Secrets are an always-A floor, applied below. Any other content
    # signal moves one step toward A.
    if any(name != "secrets" for name in signals):
        tier = toward_approve(tier)

    if request.human_categories:
        tier = toward_approve(tier)
        reasons.append("protected category: " + ",".join(request.human_categories))

    if band == "high":
        tier = toward_approve(tier)
        reasons.append("high blast radius moves the tier toward approval")

    dual_ok: bool | None = None
    reviewers: tuple[str, ...] = ()
    if band == "medium":
        dual_ok, reviewers, dual_reason = _dual_review(request)
        reasons.append(dual_reason)
        if not dual_ok:
            tier = "A"

    batch = request.batch_size if request.batch_size is not None else max(files, 1)
    brake = _tripped(batch, request.recent_batch_sizes, request.invocations_in_window)
    if brake and tier in {"N", "I"}:
        tier = "C"
        reasons.append("volume brake demotes this burst to consulted")

    scope = request.scope or scope_key(request.repo, request.branch, request.paths or tuple(
        change.path for change in changes
    ))
    first = bool(scope.strip("|")) and scope not in request.seen_scopes
    if first and tier in {"N", "I"}:
        tier = "C"
        reasons.append("first time in this scope")

    gate = _gate(request)
    if gate:
        tier = "A"
        reasons.append("CI or deploy gate weakened")

    if request.force_push_default or request.history_rewrite_default or _rewrite_in(request.diff):
        if "force-push or history rewrite of the default branch" not in floor:
            floor = (*floor, "force-push or history rewrite of the default branch")
    if floor:
        tier = "A"
        reasons.append("always-A floor: " + "; ".join(floor))

    if request.model_risk:
        reasons.append(f"model risk {request.model_risk} is advisory and did not set the tier")

    categories = _categories(
        request.human_categories,
        signals,
        floor,
        gate,
        sensitive,
    )
    approvers, needed = _roster_for(categories, config)
    priority, event = _notify(
        tier,
        categories=categories,
        brake=brake and "volume brake demotes this burst to consulted" in reasons,
        first=first and "first time in this scope" in reasons,
        floor=floor,
        gate=gate,
    )
    candidate = request.candidate or _candidate(request, changes)
    summary = _summary(tier, action, candidate, priority)
    return RiskDecision(
        tier=tier,
        action=action,
        reasons=tuple(reasons),
        priority=priority,
        event=event,
        summary=summary,
        blast_band=band,
        counted_lines=lines,
        counted_files=files,
        excluded=excluded,
        sensitive_paths=tuple(sensitive),
        content_signals=tuple(signals),
        floor=floor,
        gate=gate,
        approvers=approvers,
        approvals_required=needed,
        preset=preset,
        trust=trust,
        autonomy_enabled=request.autonomy_enabled,
        model_advisory=request.model_risk,
        dual_review_ok=dual_ok,
        reviewers=reviewers,
        volume_brake="volume brake demotes this burst to consulted" in reasons,
        first_scope="first time in this scope" in reasons,
        candidate=candidate,
        refit_notes=tuple(refit_notes),
    )


def present_preset(
    *,
    interactive: bool,
    cli: str = "axi",
    read=None,
    write=None,
    save=None,
) -> str:
    """Explain the shipped preset. A non-interactive install prints the opt-out."""
    out = write or print
    out(_PRESET_EXPLANATION.rstrip())
    opt_out = f"{cli} settings set raci.preset strict"
    if not interactive:
        out(f"Opt out: {opt_out}")
        return SHIPPED_PRESET
    out("Preset [autonomous/balanced/strict] (default autonomous):")
    raw = read() if read is not None else input()
    choice = (raw or "").strip().lower()
    if choice not in {"strict", "balanced", "autonomous"}:
        return SHIPPED_PRESET
    if choice != SHIPPED_PRESET:
        if save is not None:
            save(choice)
        else:
            _save_preset(choice)
    return choice


def shadow_state_dir(state_dir: Path | None = None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    for key in ("AXI_STATE_DIR", "AXIOM_STATE_DIR"):
        value = os.environ.get(key)
        if value:
            return Path(value)
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def shadow_log_path(state_dir: Path) -> Path:
    return Path(state_dir) / "raci" / "graduation-shadow.jsonl"


def read_shadow_log(state_dir: Path) -> list[dict]:
    path = shadow_log_path(state_dir)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def observe(
    request: ReviewRequest,
    *,
    state_dir: Path | None = None,
    live_block: str = "",
    kind: str = "hold",
) -> dict:
    """Log one shadow decision and emit its HERALD subject. Landing is untouched."""
    root = shadow_state_dir(state_dir)
    if request.seen_scopes:
        filled = request
        scope = request.scope
    else:
        seen = _read_scopes(root)
        scope = request.scope or scope_key(request.repo, request.branch, request.paths)
        filled = replace(request, seen_scopes=tuple(seen), scope=scope or request.scope)
    decision = evaluate(filled)
    record = decision.to_dict()
    record["live_block"] = live_block
    record["kind"] = kind
    record["would_change_landing"] = False
    record["gated_by_autonomy_dial"] = not decision.autonomy_enabled
    path = shadow_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    if decision.first_scope and (filled.scope or scope):
        _append_scope(root, filled.scope or scope)
    _ledger(root, decision, record)
    _emit(decision)
    return record


def deliver(decision: RiskDecision, ctx: object) -> object:
    """Hand one shadow report to HERALD ``send``. The inbox is always on."""
    from axiom.extensions.builtins.notifications.inbox import always_on_inbox
    from axiom.extensions.builtins.notifications.send import (
        NotificationPayload,
        Priority,
        send,
    )
    from axiom.governance import Classification

    recipient, _channel = always_on_inbox(decision.recipient)
    return send(
        ctx,
        actor="@rivet",
        recipient=recipient,
        payload=NotificationPayload(
            summary=decision.summary,
            metadata={"tier": decision.tier, "shadow": True, "event": decision.event},
        ),
        classification=Classification.INTERNAL,
        priority=Priority(decision.priority),
        intent="notification.send",
    )


def observe_landing(pull: object, config: object, *, action: str, live_block: str, kind: str) -> None:
    """Shadow a lander decision. Failures are logged and swallowed."""
    try:
        from axiom.extensions.builtins.release.lander.protect import config_categories

        files = tuple(getattr(pull, "files", ()) or ())
        categories = config_categories(files, config)
        preset, explicit, autonomy = _installed_posture()
        request = ReviewRequest(
            action=action,
            paths=files,
            additions=int(getattr(pull, "additions", 0) or 0),
            deletions=int(getattr(pull, "deletions", 0) or 0),
            author_model=str(getattr(pull, "author_model", "") or ""),
            review_models=tuple(getattr(config, "review_roster", ()) or ()),
            separate_reviewer=bool(getattr(config, "separate_reviewer", True)),
            branch=str(getattr(pull, "head_ref", "") or ""),
            candidate=f"pr:{getattr(pull, 'number', '')}",
            human_categories=tuple(categories),
            preset=preset,
            explicit_trust=explicit,
            autonomy_enabled=autonomy,
            config=load_review_config(),
            seen_scopes=(),
        )
        observe(request, live_block=live_block, kind=kind)
    except Exception:
        _log.debug("risk-tier shadow skipped", exc_info=True)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _save_preset(preset: str) -> None:
    from axiom.extensions.builtins.settings.store import SettingsStore

    SettingsStore().set("raci.preset", preset)


def _installed_posture() -> tuple[str, int | None, bool]:
    preset = SHIPPED_PRESET
    explicit: int | None = None
    autonomy = False
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore, autonomy_enabled

        store = SettingsStore()
        raw = str(store.get("raci.preset", SHIPPED_PRESET) or SHIPPED_PRESET)
        if raw in {"strict", "balanced", "autonomous"}:
            preset = raw
        if "raci.trust" in store._project or "raci.trust" in store._global:
            explicit = int(store.get("raci.trust"))
        autonomy = bool(autonomy_enabled())
    except Exception:
        return preset, explicit, autonomy
    return preset, explicit, autonomy


def _blast_from(raw: Mapping) -> BlastFloor:
    def _int(name: str, default: int) -> int:
        try:
            return int(raw.get(name, default))
        except (TypeError, ValueError):
            return default

    learn = raw.get("learn", True)
    return BlastFloor(
        lines_medium=_int("lines_medium", 800),
        lines_high=_int("lines_high", 2000),
        files_medium=_int("files_medium", 25),
        files_high=_int("files_high", 50),
        learn=bool(learn),
    )


def _approvers_from(raw: Mapping) -> tuple[tuple[str, ...], int, tuple[tuple[str, CategoryRoster], ...]]:
    roster = DEFAULT_APPROVERS
    count = DEFAULT_APPROVAL_COUNT
    if isinstance(raw.get("roster"), list) and raw.get("roster"):
        roster = tuple(str(item) for item in raw["roster"])
    if "count" in raw:
        try:
            count = max(1, int(raw["count"]))
        except (TypeError, ValueError):
            count = DEFAULT_APPROVAL_COUNT
    categories: list[tuple[str, CategoryRoster]] = []
    for key, value in raw.items():
        if key in {"roster", "count"} or not isinstance(value, Mapping):
            continue
        names = value.get("roster")
        if not isinstance(names, list) or not names:
            continue
        try:
            needed = max(1, int(value.get("count", count)))
        except (TypeError, ValueError):
            needed = count
        categories.append((str(key), CategoryRoster(tuple(str(item) for item in names), needed)))
    return roster, count, tuple(categories)


def _compile_glob(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("(?:^|/)" + "".join(parts) + "$")


def _matching_paths(paths: Sequence[str], patterns: Sequence[str]) -> list[str]:
    compiled = [_compile_glob(pattern) for pattern in patterns]
    hits = []
    for path in paths:
        if any(regex.search(path) for regex in compiled):
            hits.append(path)
    return hits


def _changes(request: ReviewRequest) -> tuple[FileChange, ...]:
    if request.files:
        return request.files
    parsed = _parse_diff(request.diff) if request.diff else ()
    if parsed:
        return parsed
    return tuple(FileChange(path=path) for path in request.paths)


def _parse_diff(diff: str) -> tuple[FileChange, ...]:
    if "diff --git " not in diff and not re.search(r"^\d+\t\d+\t", diff, re.M):
        return ()
    if "diff --git " not in diff:
        return _parse_numstat(diff)
    changes: list[FileChange] = []
    path = ""
    additions = 0
    deletions = 0
    rename = False
    added: list[str] = []

    def flush() -> None:
        nonlocal path, additions, deletions, rename, added
        if path:
            changes.append(FileChange(
                path=path,
                additions=additions,
                deletions=deletions,
                rename=rename and additions == 0 and deletions == 0,
                added_text="\n".join(added),
            ))
        path = ""
        additions = 0
        deletions = 0
        rename = False
        added = []

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            flush()
            marker = " b/"
            path = line.split(marker, 1)[1] if marker in line else line.split()[-1]
        elif line.startswith("rename from ") or line.startswith("similarity index 100%"):
            rename = True
        elif line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        elif line.startswith("+"):
            additions += 1
            added.append(line[1:])
            rename = False
        elif line.startswith("-"):
            deletions += 1
            rename = False
    flush()
    return tuple(changes)


def _parse_numstat(diff: str) -> tuple[FileChange, ...]:
    changes = []
    for line in diff.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        add_s, del_s, path = parts
        additions = 0 if add_s == "-" else int(add_s)
        deletions = 0 if del_s == "-" else int(del_s)
        rename = " => " in path and additions == 0 and deletions == 0
        if " => " in path:
            path = path.split(" => ")[-1].strip()
        changes.append(FileChange(path=path, additions=additions, deletions=deletions, rename=rename))
    return tuple(changes)


def _excluded(change: FileChange) -> bool:
    path = change.path.replace("\\", "/")
    name = path.rsplit("/", 1)[-1].lower()
    segments = {segment.lower() for segment in path.split("/") if segment}
    if change.rename and change.additions == 0 and change.deletions == 0:
        return True
    if name in _LOCK_NAMES or name.endswith(".lock"):
        return True
    if segments & _VENDOR_SEGMENTS:
        return True
    if segments & _GENERATED_SEGMENTS or name.endswith((".pb.go", "_pb2.py", "_pb2_grpc.py")):
        return True
    if ".generated." in name:
        return True
    if segments & _SNAPSHOT_SEGMENTS or name.endswith(".snap"):
        return True
    return False


def _measure(
    changes: Sequence[FileChange],
    additions: int,
    deletions: int,
) -> tuple[int, int, tuple[str, ...]]:
    counted: list[FileChange] = []
    excluded: list[str] = []
    for change in changes:
        if _excluded(change):
            excluded.append(change.path)
        else:
            counted.append(change)
    per_file = sum(change.additions + change.deletions for change in counted)
    if per_file:
        lines = per_file
    elif counted and (additions or deletions):
        lines = additions + deletions
    else:
        lines = 0
    return lines, len(counted), tuple(excluded)


def _band(lines: int, files: int, thresholds: BlastFloor) -> str:
    if lines > thresholds.lines_high or files > thresholds.files_high:
        return "high"
    if lines >= thresholds.lines_medium or files >= thresholds.files_medium:
        return "medium"
    return "low"


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    name = parts[-1] if parts else ""
    return "tests" in parts or name.startswith("test_") or name.endswith("_test.py")


def _content(
    request: ReviewRequest,
    changes: Sequence[FileChange],
    config: ReviewConfig,
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    signals: list[str] = []
    floor: list[str] = []
    added = "\n".join(change.added_text for change in changes if change.added_text)
    if not added.strip():
        added = _added_lines(request.diff)
    if not added.strip() and request.diff and "diff --git " not in request.diff:
        added = request.diff
    prod_sql = False
    if config.secrets and (added or request.diff):
        secret = _secrets(added or request.diff)
        if secret:
            signals.append("secrets")
            floor.append("secrets in diff")
    if config.destructive_sql and _DESTRUCTIVE_SQL.search(added or ""):
        signals.append("destructive_sql")
        prod_sql = _irreversible_prod(added, [change.path for change in changes] or list(request.paths))
        if prod_sql:
            floor.append("irreversible production data deletion")
    if config.live_stripe_price_ids and _live_price(changes, added, request.paths):
        signals.append("live_stripe_price_id")
    return tuple(dict.fromkeys(signals)), tuple(dict.fromkeys(floor)), prod_sql


def _added_lines(diff: str) -> str:
    lines = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    return "\n".join(lines)


def _secrets(text: str) -> bool:
    if _STRIPE_LIVE_SECRET.search(text):
        return True
    try:
        from axiom.infra.secret_scan import scan_text

        return bool(scan_text(text))
    except Exception:
        return False


def _irreversible_prod(text: str, paths: Sequence[str]) -> bool:
    prod_path = any(_PROD_HINT.search(path) for path in paths)
    irreversible = False
    if _IRREVERSIBLE_SQL.search(text):
        irreversible = True
    for match in _DELETE_FROM.finditer(text):
        statement = match.group(0)
        if not re.search(r"\bWHERE\b", statement, re.IGNORECASE):
            irreversible = True
    if not irreversible:
        return False
    if prod_path:
        return True
    window = text if len(text) < 2000 else text[:2000]
    return bool(_PROD_HINT.search(window))


def _live_price(changes: Sequence[FileChange], added: str, paths: Sequence[str]) -> bool:
    named = [change.path for change in changes if change.path] or list(paths)
    if named and all(_is_test(path) for path in named):
        return False
    if any(_STRIPE_PRICE.search(change.added_text) for change in changes):
        return True
    return bool(_STRIPE_PRICE.search(added))


def _rewrite_in(diff: str) -> bool:
    for line in diff.splitlines():
        body = line[1:] if line.startswith("+") and not line.startswith("+++") else ""
        if not body:
            continue
        if _FORCE_PUSH.search(body) and _DEFAULT_BRANCH.search(body):
            return True
        if _REWRITE.search(body):
            return True
    return False


def _dual_review(request: ReviewRequest) -> tuple[bool, tuple[str, ...], str]:
    if not request.separate_reviewer:
        return False, (), "medium blast needs two independent model reviews; separate_reviewer is off"
    try:
        from axiom.extensions.builtins.release.ci_flow import independent_reviewers

        result = independent_reviewers(
            request.author_model,
            list(request.review_models),
            count=2,
        )
    except Exception as exc:
        return False, (), f"medium blast needs two independent model reviews ({exc})"
    reviewers = tuple(result.get("reviewers") or ())
    if result.get("ok"):
        return True, reviewers, "two independent model reviews: " + ", ".join(reviewers)
    reason = result.get("reason") or "a second reviewer is not available"
    return False, reviewers, f"medium blast needs two independent model reviews; {reason}"


def _median(values: Sequence[int]) -> float:
    ordered = sorted(int(value) for value in values)
    count = len(ordered)
    if count == 0:
        return 0
    mid = count // 2
    if count % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def _tripped(batch: int, recent: Sequence[int], invocations: int) -> bool:
    if invocations > AGENT_ACTION_DEFAULT_MAX_PER_TICK:
        return True
    median = _median(recent)
    if median <= 0:
        return False
    return batch > _VOLUME_FACTOR * median


def _gate(request: ReviewRequest) -> tuple[str, ...]:
    found = [f"removed required check {name}" for name in request.removed_required_checks]
    found.extend(f"loosened deploy gate {name}" for name in request.loosened_deploy_gates)
    if not request.diff:
        return tuple(found)
    removed, added = _diff_sides(request.diff)
    for key in _CHECK_KEYS:
        if any(key in line for line in removed) and not any(key in line for line in added):
            found.append(f"removed required check {key}")
    for key in _DEPLOY_KEYS:
        if any(key in line for line in removed) and not any(key in line for line in added):
            found.append(f"loosened deploy gate {key}")
        old = _numbers(removed, key)
        new = _numbers(added, key)
        if old and new and min(new) < max(old):
            found.append(f"loosened deploy gate {key} {max(old)} -> {min(new)}")
    if any("required_status_checks" in line for line in removed + added):
        removed_items = _yaml_items(removed)
        added_items = set(_yaml_items(added))
        for item in removed_items:
            if item not in added_items:
                found.append(f"removed required check {item}")
    return tuple(dict.fromkeys(found))


def _diff_sides(diff: str) -> tuple[list[str], list[str]]:
    removed: list[str] = []
    added: list[str] = []
    for line in diff.splitlines():
        if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
    return removed, added


def _numbers(lines: Sequence[str], key: str) -> list[int]:
    found = []
    pattern = re.compile(rf"{re.escape(key)}\D{{0,16}}(\d+)")
    for line in lines:
        match = pattern.search(line)
        if match:
            found.append(int(match.group(1)))
    return found


def _yaml_items(lines: Sequence[str]) -> list[str]:
    items = []
    pattern = re.compile(r"^\s*-\s+['\"]?([^'\"]+?)['\"]?\s*$")
    for line in lines:
        match = pattern.match(line)
        if match:
            items.append(match.group(1).strip())
    return items


def _categories(
    human: Sequence[str],
    signals: Sequence[str],
    floor: Sequence[str],
    gate: Sequence[str],
    sensitive: Sequence[str] = (),
) -> tuple[str, ...]:
    found = list(human)
    found.extend(_categories_for_paths(sensitive))
    if "live_stripe_price_id" in signals or any("billing" in item or "pricing" in item for item in human):
        found.append("billing")
    if "secrets" in signals or "security" in human:
        found.append("security")
    if any("production data" in item for item in floor):
        found.append("prod")
    if any(item.startswith("loosened deploy") for item in gate):
        found.append("prod")
    return tuple(dict.fromkeys(found))


def _categories_for_paths(paths: Sequence[str]) -> list[str]:
    found: list[str] = []
    for path in paths:
        segments = [segment.lower() for segment in path.replace("\\", "/").split("/") if segment]
        name = segments[-1] if segments else ""
        if any(segment in {"auth", "crypto", "identity", "secrets", "secret"} for segment in segments):
            found.append("security")
        if "secret" in name or "credential" in name:
            found.append("security")
        if any(segment in {"billing", "pricing"} for segment in segments) or "stripe" in name:
            found.append("billing")
    return found


def _roster_for(categories: Sequence[str], config: ReviewConfig) -> tuple[tuple[str, ...], int]:
    table = dict(config.category_approvers)
    chosen: list[str] = []
    counts: list[int] = []
    for category in categories:
        specific = table.get(category)
        if specific is None:
            continue
        chosen.extend(specific.roster)
        counts.append(specific.count)
    if not chosen:
        return config.approvers, config.approval_count
    return tuple(dict.fromkeys(chosen)), max(counts)


def _notify(
    tier: str,
    *,
    categories: Sequence[str],
    brake: bool,
    first: bool,
    floor: Sequence[str],
    gate: Sequence[str],
) -> tuple[str, str]:
    urgent = bool(set(categories) & _URGENT_CATEGORIES) or any(
        "secrets" in item or "production data" in item for item in floor
    )
    if urgent and tier in {"A", "C"}:
        return "urgent", "rivet.land.approval_needed"
    if any(item.startswith("loosened deploy") for item in gate) and tier == "A":
        return "urgent", "rivet.land.approval_needed"
    if floor and tier == "A":
        return "high", "rivet.land.approval_needed"
    if any(item.startswith("removed required") for item in gate) and tier == "A":
        return "high", "rivet.land.approval_needed"
    if brake or first:
        return "normal", "rivet.land.approval_needed"
    if tier in {"A", "C"}:
        return "high", "rivet.land.approval_needed"
    return "low", "rivet.land.n_digest"


def _summary(tier: str, action: str, candidate: str, priority: str) -> str:
    verb = action.removeprefix("pr.").replace("-", " ")
    if tier in {"N", "I"}:
        return (
            f"Rivet would {verb} {candidate} at tier {tier} "
            "(shadow; landing unchanged)"
        )
    if priority == "normal":
        return f"Rivet would consult before {verb} {candidate} (shadow; landing unchanged)"
    return f"Rivet would ask for approval to {verb} {candidate} at tier {tier} (shadow; landing unchanged)"


def _candidate(request: ReviewRequest, changes: Sequence[FileChange]) -> str:
    if request.paths:
        return request.paths[0] if len(request.paths) == 1 else f"{len(request.paths)} files"
    if changes:
        return changes[0].path if len(changes) == 1 else f"{len(changes)} files"
    return request.action


def _read_scopes(state_dir: Path) -> set[str]:
    path = Path(state_dir) / "raci" / "graduation-scopes.txt"
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def _append_scope(state_dir: Path, scope: str) -> None:
    path = Path(state_dir) / "raci" / "graduation-scopes.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_scopes(state_dir)
    if scope in existing:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(scope + "\n")


def _ledger(state_dir: Path, decision: RiskDecision, record: dict) -> None:
    try:
        from axiom.policy.action_ledger import ActionLedger

        ActionLedger(state_dir=state_dir).record_action(
            agent="rivet",
            op_class="raci.risk_tier",
            name=decision.action,
            candidate=decision.candidate or decision.action,
            guards=["shadow", f"tier:{decision.tier}"],
            outcome="shadow",
            dry_run=True,
            metadata={
                "tier": decision.tier,
                "priority": decision.priority,
                "event": decision.event,
                "reasons": list(decision.reasons),
                "floor": list(decision.floor),
                "live_effect": decision.live_effect,
                "live_block": record.get("live_block", ""),
            },
        )
    except Exception:
        _log.warning("risk-tier shadow ledger write failed", exc_info=True)


def _emit(decision: RiskDecision) -> None:
    if not decision.event:
        return
    try:
        from axiom.extensions.builtins.release.lifecycle_events import emit

        emit(decision.event, {
            "summary": decision.summary,
            "priority": decision.priority,
            "tier": decision.tier,
            "action": decision.action,
            "candidate": decision.candidate,
            "shadow": True,
        })
    except Exception:
        _log.debug("risk-tier shadow emit skipped", exc_info=True)


__all__ = [
    "DEFAULT_APPROVERS",
    "BlastFloor",
    "FileChange",
    "ReviewConfig",
    "ReviewRequest",
    "RiskDecision",
    "deliver",
    "discover_raci_toml",
    "evaluate",
    "load_review_config",
    "observe",
    "observe_landing",
    "present_preset",
    "read_shadow_log",
    "refit_thresholds",
    "review_config_from_mapping",
    "scope_key",
    "shadow_log_path",
    "shadow_state_dir",
]
