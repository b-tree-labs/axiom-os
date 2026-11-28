# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-repo lander configuration.

Nothing in the defaults names a repository or an organisation. A checkout
reads ``.axiom/release-lander.toml``. An operator file for one repo lives
at ``<state>/agents/rivet/lander/<owner>__<name>.toml``. The watch list
is the CLI, then an environment variable, then ``repos`` in that file,
then the state-dir list. A mode that would merge or enqueue without an
approved proposal is coerced to warn.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from axiom.extensions.builtins.release.lander.grouping import DEFAULT_SIGNALS
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.protect import DEPENDENCY_PATTERNS

_DISARMED = {"land", "merge", "enqueue"}
_ALLOWED = {"warn", "propose"}
_ACTIONS = frozenset(
    {
        "repair",
        "batch",
        "update_branch",
        "merge",
        "enqueue",
        "close",
        "rerun",
        "drift_repair",
        "ci_start",
    }
)
_SUITE = frozenset({"default", "learned", "configured"})
_TOKEN_VALUE = re.compile(r"(ghp_|github_pat_|glpat-)")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

_DEFAULT_SETUP = (
    r"(^|/)conftest\.py$",
    r"(^|/)pytest\.ini$",
    r"(^|/)tox\.ini$",
    r"(^|/)noxfile\.py$",
    r"(^|/)\.github/workflows/[^/]+$",
    r"(^|/)\.gitlab-ci\.yml$",
)
_DEFAULT_DEPS = DEPENDENCY_PATTERNS


@dataclass(frozen=True)
class LanderConfig:
    mode: str = "warn"
    #: Whether ``land --act`` may merge anything (#2238). ``mode`` is the report posture and
    #: never gated landing; this is the explicit switch. True so a repository that relies on
    #: landing changes nothing; false lands nothing, however green a pull request is.
    land: bool = True
    language: str = "python"
    test_command: str = "pytest"
    forge: str = "github"
    window_hours: float = 6
    post_status: bool = False
    suite_cost: str = "default"
    suite_minutes: int | None = None
    repair_cap: int = 3
    max_rounds: int = 3
    pr_limit: int = 100
    claim_ttl_s: int = 3600
    approval_ttl_hours: float = 72
    review_roster: tuple[str, ...] = ()
    extra_path_rules: dict[str, tuple[str, ...]] = field(default_factory=dict)
    human_paths_mode: str = "extend"
    signals: frozenset[str] = DEFAULT_SIGNALS
    selector: str = "testmon"
    selector_database: str = ".testmondata"
    test_setup_patterns: tuple[str, ...] = _DEFAULT_SETUP
    dependency_patterns: tuple[str, ...] = _DEFAULT_DEPS
    full_suite_contexts: tuple[str, ...] = ("merge-queue", "main", "nightly")
    fix_provider: str = "mechanical"
    webhook_url_env: str = ""
    webhook_key_env: str = ""
    webhook_key_header: str = "X-Fix-Key"
    separate_reviewer: bool = True
    auto_approver: str = ""
    self_landers: tuple[str, ...] = ()
    claim_label: str = "rivet:claimed"
    foreign_claim_labels: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    repos_env: str = "AXIOM_LANDER_REPOS"
    token_env: str = "GH_TOKEN"
    token_scopes: tuple[str, ...] = ("repo",)
    approvers: tuple[str, ...] = ()
    auto_allowed: tuple[str, ...] = ()
    needs_approval: tuple[str, ...] = tuple(sorted(_ACTIONS))
    kill_switch_env: str = "AXIOM_LANDER_KILL"
    #: Minutes a head may sit with no check and no workflow run before RIVET
    #: says CI never ran on it (``[ci_start] grace_minutes``).
    ci_start_grace_minutes: float = 30
    #: Environment variable holding the token that closes and reopens a pull
    #: request to start its CI (``[ci_start] token_env``). Empty uses ``token_env``.
    #: A forge that starts no workflow for its own job token needs another one here.
    ci_start_token_env: str = ""
    #: Labels that hold a pull request for its owner (``[hold] labels``): finished,
    #: not to be landed or touched until the label is gone (``lander.owner_hold``).
    hold_labels: tuple[str, ...] = ("hold: owner review",)
    #: A pending review request to an owner is a hold too (``[hold] owner_review_request``).
    hold_on_owner_review_request: bool = True
    #: Logins that count as an owner for that (``[hold] owners``). Empty means the
    #: account the repository belongs to.
    hold_owners: tuple[str, ...] = ()
    land_method: str = "merge"
    #: The landing queue (#2399): ``off``, ``shadow`` or ``on`` (``[queue] mode``).
    queue_mode: str = "off"
    #: Candidates tested at once (``[queue] concurrency``).
    queue_concurrency: int = 1
    #: The most pull requests one merged state would hold (``[queue] batch_size``).
    queue_batch_size: int = 4
    #: The owner has paused the queue (``[queue] paused``).
    queue_paused: bool = False
    notes: tuple[str, ...] = ()
    source: str = ""

    @property
    def replaces_human_paths(self) -> bool:
        """True when ``[human_paths]`` is the entire protected set."""
        return self.human_paths_mode == "replace"


def _fix_provider(data: dict, language: str, notes: list[str]) -> tuple[str, dict, bool]:
    """A provider name, or a table with ``name`` and ``webhook``.

    A string and a table cannot share one TOML key, so the webhook lives
    under ``[fix_provider]`` when the handoff is enabled.
    """
    default = "mechanical" if language == "python" else "null"
    raw = data.get("fix_provider")
    if raw is None:
        return default, {}, True
    if isinstance(raw, str):
        return (raw.strip() or default), {}, True
    if not isinstance(raw, dict):
        raise LanderRefused("fix_provider must be a name or a table")
    name = str(raw.get("name") or default).strip() or default
    webhook = raw.get("webhook") or {}
    if webhook and not isinstance(webhook, dict):
        raise LanderRefused("fix_provider.webhook must be a table")
    webhook = dict(webhook)
    for field_name in ("url_env", "key_env"):
        value = str(webhook.get(field_name) or "")
        if value and not _ENV_NAME.match(value):
            raise LanderRefused(
                f"fix_provider.webhook.{field_name} must be an environment variable name"
            )
    header = str(webhook.get("key_header") or "X-Fix-Key")
    if not header or any(char.isspace() for char in header) or header.startswith("-"):
        raise LanderRefused("fix_provider.webhook.key_header must be a header name")
    separate = webhook.get("separate_reviewer", True)
    if separate is not True:
        notes.append("separate_reviewer stays on; the reviewer must differ from the fix writer")
    return name, webhook, True


def _claim_ttl(data: dict, claims: dict) -> int:
    """Seconds a claim is held. The claims table wins over a top-level key."""
    if "claim_ttl_s" in claims:
        raw = claims.get("claim_ttl_s")
        field_name = "claims.claim_ttl_s"
    else:
        raw = data.get("claim_ttl_s")
        field_name = "claim_ttl_s"
    if raw is None or raw == "":
        return 3600
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise LanderRefused(f"{field_name} must be a positive number of seconds")
    if raw < 1:
        raise LanderRefused(f"{field_name} must be a positive number of seconds")
    return raw


def _strings(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise LanderRefused(f"{field_name} must be a list of strings")
    return tuple(item.strip() for item in value)


def _reject_secrets(data: dict) -> None:
    blob = str(data)
    if _TOKEN_VALUE.search(blob):
        raise LanderRefused("config must name a token environment variable, not a token value")


def parse_config(text: str, *, source: str = "") -> LanderConfig:
    data = tomllib.loads(text)
    if not isinstance(data, dict):
        raise LanderRefused("lander config must be a TOML table")
    _reject_secrets(data)
    raw_mode = str(data.get("mode") or "warn")
    notes: list[str] = []
    if raw_mode in _DISARMED:
        notes.append(f"mode {raw_mode!r} is not enabled; running warn until a proposal is approved")
        mode = "warn"
    elif raw_mode in _ALLOWED:
        mode = raw_mode
    else:
        raise LanderRefused(f"unknown lander mode {raw_mode!r}")
    land = data.get("land", True)
    if not isinstance(land, bool):
        raise LanderRefused("land must be true or false")
    suite_cost = str(data.get("suite_cost") or "default")
    if suite_cost not in _SUITE:
        raise LanderRefused(f"unknown suite_cost {suite_cost!r}")
    suite = data.get("suite_minutes")
    suite_minutes = int(suite) if isinstance(suite, int) and suite > 0 else None
    if suite_cost == "configured" and not suite_minutes:
        raise LanderRefused("suite_cost = configured needs suite_minutes")
    language = str(data.get("language") or "python")
    selector = str(data.get("selector") or ("testmon" if language == "python" else "null"))
    fix_provider, webhook, separate_reviewer = _fix_provider(data, language, notes)
    forge = str(data.get("forge") or "github")
    land_method = str(data.get("land_method") or "merge")
    if land_method not in {"merge", "enqueue"}:
        raise LanderRefused(f"unknown land_method {land_method!r}")
    review = data.get("review") or {}
    roster = review.get("roster") if isinstance(review, dict) else ()
    paths = data.get("human_paths") or {}
    extra: dict[str, tuple[str, ...]] = {}
    if paths and not isinstance(paths, dict):
        raise LanderRefused("human_paths must be a table of categories")
    if isinstance(paths, dict):
        for key, value in paths.items():
            extra[str(key)] = _strings(value, field_name=f"human_paths.{key}")
    human_mode = str(data.get("human_paths_mode") or "extend")
    if human_mode not in {"extend", "replace"}:
        raise LanderRefused("human_paths_mode must be extend or replace")
    if human_mode == "replace":
        # The table becomes the entire protected set. An empty one would
        # protect nothing, and a pattern that does not compile would fail
        # at match time, so both are refused here.
        if not any(extra.values()):
            raise LanderRefused(
                "human_paths_mode = replace needs a non-empty [human_paths] table; "
                "an empty replacement would protect nothing"
            )
        for key, patterns in extra.items():
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise LanderRefused(
                        f"human_paths.{key} has a pattern that does not compile: {exc}"
                    ) from exc
    signals = data.get("signals")
    if signals is None:
        chosen_signals = DEFAULT_SIGNALS
    else:
        chosen_signals = frozenset(_strings(signals, field_name="signals"))
        unknown = chosen_signals - DEFAULT_SIGNALS
        if unknown:
            raise LanderRefused(f"unknown batching signals: {', '.join(sorted(unknown))}")
    selector_table = data.get("selector_options") or {}
    if selector_table and not isinstance(selector_table, dict):
        raise LanderRefused("selector_options must be a table")
    token = data.get("token") or {}
    if token and not isinstance(token, dict):
        raise LanderRefused("token must be a table")
    token_env = str((token or {}).get("env") or "GH_TOKEN")
    if not _ENV_NAME.match(token_env):
        raise LanderRefused("token.env must be an environment variable name")
    watch = data.get("watch") or {}
    repos_env = str((watch or {}).get("env") or data.get("repos_env") or "AXIOM_LANDER_REPOS")
    if not _ENV_NAME.match(repos_env):
        raise LanderRefused("watch.env must be an environment variable name")
    raci = data.get("raci") or {}
    if raci and not isinstance(raci, dict):
        raise LanderRefused("raci must be a table")
    approvers = _strings((raci or {}).get("approvers"), field_name="raci.approvers")
    auto_allowed = _strings((raci or {}).get("auto_allowed"), field_name="raci.auto_allowed")
    if set(auto_allowed) - _ACTIONS:
        raise LanderRefused("raci.auto_allowed names an unknown action")
    needs = (raci or {}).get("needs_approval")
    needs_approval = (
        tuple(sorted(_ACTIONS))
        if needs is None
        else _strings(needs, field_name="raci.needs_approval")
    )
    if set(needs_approval) - _ACTIONS:
        raise LanderRefused("raci.needs_approval names an unknown action")
    kill_env = str((raci or {}).get("kill_switch_env") or "AXIOM_LANDER_KILL")
    if not _ENV_NAME.match(kill_env):
        raise LanderRefused("raci.kill_switch_env must be an environment variable name")
    auto_approver = str((raci or {}).get("auto_approver") or "").strip()
    self_landers = _strings(data.get("self_landers"), field_name="self_landers")
    claims = data.get("claims") or {}
    if claims and not isinstance(claims, dict):
        raise LanderRefused("claims must be a table")
    claim_label = str((claims or {}).get("label") or "rivet:claimed").strip() or "rivet:claimed"
    if any(char.isspace() for char in claim_label):
        raise LanderRefused("claims.label must be a single label")
    foreign_claim_labels = _strings(
        (claims or {}).get("foreign_labels"), field_name="claims.foreign_labels"
    )
    if claim_label in foreign_claim_labels:
        raise LanderRefused("claims.label must not also be a foreign claim label")
    ci_start = data.get("ci_start") or {}
    if not isinstance(ci_start, dict):
        raise LanderRefused("ci_start must be a table")
    grace = ci_start.get("grace_minutes", 30)
    if isinstance(grace, bool) or not isinstance(grace, (int, float)) or grace <= 0:
        raise LanderRefused("ci_start.grace_minutes must be a positive number of minutes")
    ci_start_token_env = str(ci_start.get("token_env") or "")
    if ci_start_token_env and not _ENV_NAME.match(ci_start_token_env):
        raise LanderRefused("ci_start.token_env must be an environment variable name")
    hold = data.get("hold")
    if hold is None:
        hold = {}
    if not isinstance(hold, dict):
        raise LanderRefused("hold must be a table")
    hold_labels = ("hold: owner review",)
    if "labels" in hold:
        hold_labels = _strings(hold.get("labels"), field_name="hold.labels")
        if not hold_labels:
            raise LanderRefused("hold.labels must name at least one label")
    if claim_label.casefold() in {label.casefold() for label in hold_labels}:
        raise LanderRefused("hold.labels must not include the claim label")
    hold_on_request = hold.get("owner_review_request", True)
    if not isinstance(hold_on_request, bool):
        raise LanderRefused("hold.owner_review_request must be true or false")
    hold_owners = _strings(hold.get("owners"), field_name="hold.owners")
    queue = data.get("queue")
    if queue is None:
        queue = {}
    if not isinstance(queue, dict):
        raise LanderRefused("queue must be a table")
    queue_mode = str(queue.get("mode") or "off")
    if queue_mode not in {"off", "shadow", "on"}:
        raise LanderRefused(f"unknown queue mode {queue_mode!r}")
    queue_concurrency = queue.get("concurrency", 1)
    if (
        isinstance(queue_concurrency, bool)
        or not isinstance(queue_concurrency, int)
        or queue_concurrency < 1
    ):
        raise LanderRefused("queue.concurrency must be a positive whole number")
    queue_batch_size = queue.get("batch_size", 4)
    if (
        isinstance(queue_batch_size, bool)
        or not isinstance(queue_batch_size, int)
        or queue_batch_size < 1
    ):
        raise LanderRefused("queue.batch_size must be a positive whole number")
    queue_paused = queue.get("paused", False)
    if not isinstance(queue_paused, bool):
        raise LanderRefused("queue.paused must be true or false")
    max_rounds = int((raci or {}).get("max_rounds") or data.get("max_rounds") or 3)
    if max_rounds < 1:
        raise LanderRefused("max_rounds must be at least 1")
    scopes = _strings((token or {}).get("scopes"), field_name="token.scopes") or ("repo",)
    setup = (
        _strings((selector_table or {}).get("test_setup"), field_name="selector_options.test_setup")
        or _DEFAULT_SETUP
    )
    deps = (
        _strings(
            (selector_table or {}).get("dependencies"), field_name="selector_options.dependencies"
        )
        or _DEFAULT_DEPS
    )
    full = _strings(
        (selector_table or {}).get("full_contexts"), field_name="selector_options.full_contexts"
    ) or (
        "merge-queue",
        "main",
        "nightly",
    )
    return LanderConfig(
        mode=mode,
        land=land,
        language=language,
        test_command=str(data.get("test_command") or "pytest"),
        forge=forge,
        window_hours=float(data.get("window_hours") or 6),
        post_status=bool(data.get("post_status") or False),
        suite_cost=suite_cost,
        suite_minutes=suite_minutes,
        repair_cap=int(data.get("repair_cap") or (raci or {}).get("repair_cap") or 3),
        max_rounds=max_rounds,
        pr_limit=int(data.get("pr_limit") or 100),
        claim_ttl_s=_claim_ttl(data, claims if isinstance(claims, dict) else {}),
        approval_ttl_hours=float((raci or {}).get("approval_ttl_hours") or 72),
        review_roster=tuple(str(item) for item in (roster or ())),
        extra_path_rules=extra,
        human_paths_mode=human_mode,
        signals=chosen_signals,
        selector=selector,
        selector_database=str((selector_table or {}).get("database") or ".testmondata"),
        test_setup_patterns=setup,
        dependency_patterns=deps,
        full_suite_contexts=full,
        fix_provider=fix_provider,
        webhook_url_env=str(webhook.get("url_env") or ""),
        webhook_key_env=str(webhook.get("key_env") or ""),
        webhook_key_header=str(webhook.get("key_header") or "X-Fix-Key"),
        separate_reviewer=separate_reviewer,
        auto_approver=auto_approver,
        self_landers=self_landers,
        claim_label=claim_label,
        foreign_claim_labels=foreign_claim_labels,
        repos=_strings(data.get("repos"), field_name="repos"),
        repos_env=repos_env,
        token_env=token_env,
        token_scopes=scopes,
        approvers=approvers,
        auto_allowed=auto_allowed,
        needs_approval=needs_approval,
        kill_switch_env=kill_env,
        ci_start_grace_minutes=float(grace),
        ci_start_token_env=ci_start_token_env,
        hold_labels=hold_labels,
        hold_on_owner_review_request=hold_on_request,
        hold_owners=hold_owners,
        land_method=land_method,
        queue_mode=queue_mode,
        queue_concurrency=queue_concurrency,
        queue_batch_size=queue_batch_size,
        queue_paused=queue_paused,
        notes=tuple(notes),
        source=source,
    )


def load_config(
    state_dir: Path,
    repo: str,
    *,
    explicit: Path | None = None,
) -> LanderConfig:
    """Load the first config that exists. ``explicit`` is required to exist."""
    if explicit is not None:
        path = Path(explicit)
        return parse_config(path.read_text(encoding="utf-8"), source=str(path))
    safe = repo.replace("/", "__")
    candidates = (
        Path(state_dir) / "agents" / "rivet" / "lander" / f"{safe}.toml",
        Path(".axiom") / "release-lander.toml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return parse_config(candidate.read_text(encoding="utf-8"), source=str(candidate))
    return LanderConfig()


def configured_repos(state_dir: Path) -> tuple[str, ...]:
    """``lander-repos``, one ``owner/name`` per line, then the one watch list.

    No default repo: an empty list stays empty.
    """
    from axiom.extensions.builtins.release.watch_list import names

    path = Path(state_dir) / "agents" / "rivet" / "lander-repos"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    listed = tuple(
        line.strip() for line in lines if line.strip() and not line.strip().startswith("#")
    )
    return listed or names(state_dir)


def watch_repos(
    state_dir: Path,
    config: LanderConfig | None = None,
    *,
    cli: tuple[str, ...] | list[str] = (),
    environ: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """CLI, then the configured environment variable, then the file, then state.

    The first source that names a repo wins. Nothing is assumed.
    """
    config = config or LanderConfig()
    named = tuple(item.strip() for item in cli if item and item.strip())
    if named:
        return named
    env = os.environ if environ is None else environ
    raw = env.get(config.repos_env, "").strip()
    if raw:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if config.repos:
        return config.repos
    return configured_repos(state_dir)
