# spec-agent-action-guard

**Status:** Active (Axiom 0.21+)
**Owner:** Axiom platform
**Implementation:** `src/axiom/policy/agent_action_guard.py`

## Purpose

Policy middleware for **autonomous destructive agent actions**. An agent action that mutates external state (close a GitHub issue, delete a branch, publish a wheel, send a notification) should not roll its own env-var checks, per-call rate limit, or kill-switch logic. This module is the canonical substrate.

The framework guards **reversible** destructive ops. Per ADR-045 D6.2, irreversible ops (force-push, publish, non-ancestor delete) are now **refused** by the reversibility gate — they never graduate past human approval, so they must not flow through the autonomous guard; their consumers declare `reversible=False` and route to RACI `C`. The remaining D6 primitives (learned-baseline circuit-breaker, novelty/approval-on-first, act-then-notify digest) are the graduation-engine follow-on; the static `volume_mode="confirm"` downgrade ships today.

The framework grew out of the 2026-05-22 question: *"how can we be sure RIVET will never make a catastrophic update?"* (see issues [#218](https://github.com/b-tree-labs/axiom-os/issues/218) and [#219](https://github.com/b-tree-labs/axiom-os/issues/219)). The auto-closer was guarded by ad-hoc `if _is_disabled(): return []` style checks. Without a substrate, every new destructive surface would have to reimplement the same defenses (and would inevitably miss one).

## Contract

### `AgentAction` — declarative shape

```python
@dataclass(frozen=True)
class AgentAction:
    agent: str                # e.g. "rivet"
    op_class: str             # e.g. "github.issue.close"
    name: str                 # specific action, e.g. "auto_close_on_recovery"
    candidates: list[Any] = []
    reversible: bool = True
    metadata: dict = {}
```

`agent` + `op_class` together form the env-var prefix and the pause-sentinel scope. Pick `op_class` strings carefully — they're part of the operator-facing API surface (`RIVET_GITHUB_ISSUE_CLOSE_DISABLE`, `pause.github.issue.close.json`).

### `guarded_act(...)` — the composer

```python
decision = guarded_act(
    action,
    do_one=lambda candidate: ...,        # invoked per candidate; returns bool
    state_dir=get_user_state_dir(),
    state_probes=[lambda: (main_passing(), "main not passing")],
    env_aliases={"RIVET_AUTO_CLOSE=0": "disable"},  # back-compat
    notify_refusal=sink.send,            # called when volume guard refuses
    dry_run=False,                       # CLI --dry-run override
    volume_mode="refuse",                # "refuse" | "confirm" | "off" (D6.3)
)
```

Guard order (composable; first refusal short-circuits):

1. **Hard disable (env var)** — `<AGENT>_<OP_CLASS>_DISABLE=1` (auto-derived from action). Legacy aliases honored via `env_aliases`.
2. **Sentinel-file pause** — `<state_dir>/agents/<agent>/pause.<scope>.json` exists. Scope is `"all"` (any op for this agent) or the exact `op_class`.
3. **State preconditions** — caller-supplied `state_probes`. First failing probe short-circuits with its reason.
4. **Reversibility gate (ADR-045 D6.2)** — if `action.reversible` is False, refuse before acting (even under dry-run) with `reason="irreversible..."`. Irreversible autonomous ops route to human approval, never through the guard.
5. **Volume bound** — `len(candidates) > limit` (`<AGENT>_<OP_CLASS>_MAX_PER_TICK`, default `AGENT_ACTION_DEFAULT_MAX_PER_TICK = 10`). Behaviour depends on `volume_mode`:
   - `"refuse"` (default) — refuse the **entire batch** (`reason="volume_limit_exceeded..."`), `notify_refusal` called. Partial action muddies the audit.
   - `"confirm"` (D6.3) — **downgrade** to `proceed=False, would_proceed=candidates, reason="needs_confirmation:volume ..."`; the consumer prompts and re-runs confirmed.
   - `"off"` — skip the volume gate (used after the operator has confirmed an over-limit batch).
6. **Dry-run** — `<AGENT>_<OP_CLASS>_DRY_RUN=1` or the explicit `dry_run=True` kwarg. Returns `proceed=True, would_proceed=candidates, reason="dry_run"` without calling `do_one`.
7. **Per-candidate execution** — `do_one(candidate)` invoked for each; success → `completed`, failure (returns False or raises) → `refused`.

### `GuardDecision` — outcome shape

```python
@dataclass(frozen=True)
class GuardDecision:
    proceed: bool
    completed: list[Any] = []           # candidates do_one succeeded on
    refused: list[Any] = []             # per-candidate failures OR full batch refused
    would_proceed: list[Any] = []       # dry-run only
    reason: str = ""                    # "hard_disable" / "paused:X" /
                                         # "irreversible..." / state-probe reason /
                                         # "volume_limit_exceeded ..." /
                                         # "needs_confirmation:volume ..." / "dry_run"
```

### `is_action_disabled(action, *, state_dir, env_aliases)`

Cheap pre-flight check. Returns `True` when the agent is hard-disabled or paused. Consumers use this to short-circuit before doing expensive candidate enumeration (a `gh issue list` call that would be wasted).

### `pause_action(...)` / `resume_action(...)` / `list_paused(...)`

Helpers for the operator-facing pause sentinel. Each agent ships its own friendly CLI verb that calls these (`axi release pause --scope auto-close` maps `auto-close` → `op_class=github.issue.close`).

## Env-var schema

| Variable | Effect |
|---|---|
| `<AGENT>_<OP_CLASS>_DISABLE=1` | Hard disable this op. `is_action_disabled()` returns `True`; `guarded_act` returns `proceed=False, reason="hard_disable"`. |
| `<AGENT>_<OP_CLASS>_DRY_RUN=1` | Dry-run. `guarded_act` returns `proceed=True, would_proceed=candidates, reason="dry_run"`. |
| `<AGENT>_<OP_CLASS>_MAX_PER_TICK=N` | Override the per-invocation volume cap (default 10). |

`<OP_CLASS>` is upper-cased with `.` → `_` (so `github.issue.close` → `GITHUB_ISSUE_CLOSE`).

`env_aliases` lets consumers honor existing operator-facing env vars without renaming them. Map legacy env-var-string → role (`"disable"` or `"dry_run"`). Format: `"NAME=VALUE"` for exact match, or `"NAME"` for `value=="1"`.

## Sentinel-file format

Location: `<state_dir>/agents/<agent>/pause.<scope>.json`

```json
{
  "paused_at": "2026-05-22T20:09:07Z",
  "paused_by": "ben",
  "scope": "github.issue.close",
  "reason": "weird recovery flap, investigating"
}
```

`scope` is one of:
- `"all"` — halts every `op_class` for this agent
- A specific `op_class` (e.g. `"github.issue.close"`)

Sentinels persist across reboots — the safety stance is **paused until explicitly resumed**, not paused-until-process-restart.

## Worked example — the recovery auto-closer

```python
# src/axiom/extensions/builtins/release/pr_check_auto_closer.py

_ENV_ALIASES = {
    "RIVET_AUTO_CLOSE=0": "disable",          # legacy operator env
    "RIVET_AUTO_CLOSE_DRY_RUN=1": "dry_run",
}


def auto_close_on_recovery(flip: StateFlip) -> list[StaleIssue]:
    if not (flip.to_state == "passing" and flip.from_state == "failing"):
        return []

    # Cheap pre-flight: if RIVET is hard-disabled / paused, don't even
    # enumerate candidates.
    probe = AgentAction(agent="rivet", op_class="github.issue.close",
                        name="auto_close_on_recovery")
    if is_action_disabled(
        probe, state_dir=get_user_state_dir(), env_aliases=_ENV_ALIASES,
    ):
        return []

    stale = find_stale_pr_issues(pr_number=flip.pr_number)
    if not stale:
        return []

    action = AgentAction(
        agent="rivet", op_class="github.issue.close",
        name="auto_close_on_recovery",
        candidates=stale,
        metadata={"flip_url": flip.url, "pr_number": flip.pr_number},
    )
    decision = guarded_act(
        action,
        do_one=lambda issue: close_stale_issue(
            issue_number=issue.number, comment=...,
        ),
        state_dir=get_user_state_dir(),
        env_aliases=_ENV_ALIASES,
    )
    if decision.reason == "dry_run":
        return decision.would_proceed
    return decision.completed
```

## Patterns to use

- **One framework call per logical batch.** Don't call `guarded_act` inside a loop over candidates — pass the whole batch in. The volume guard exists for exactly this case.
- **Always pass `state_dir=get_user_state_dir()`** so sentinels work. Tests can pass `tmp_path`.
- **Wire `notify_refusal`** to the agent's notification path. The volume-refusal notification is the *only* signal the operator gets that an unusual batch was refused.
- **Use `env_aliases` for back-compat** when an env var is already operator-documented. Don't break the contract.

## Patterns to avoid

- **Don't roll your own kill switches.** Use `is_action_disabled()` + `pause_action()`. Consistency matters more than local cleverness.
- **Don't bypass the framework for "small" actions.** If it mutates external state and is autonomous, route through `guarded_act`. The 5-line "just this once" exception is how surfaces drift over time.
- **Don't set env vars from inside the framework consumer.** Pass `dry_run=True` as a kwarg instead of `os.environ[...] = "1"`. Env-var mutation has cross-process side effects.
- **Don't lower the default volume cap below 5** without justification. Steady-state recovery sees 0-3 closures per tick; 10 is generous-but-watchful. Lower caps cause false-refusal pain on legitimate small bursts.

## Future extensions

Not implemented today, but the framework shape accommodates them:

- **Approval-on-first** — the first time an agent fires a given `(agent, op_class, name)` triple, refuse and notify; require an explicit confirm to allow it. Trust-builds in the early period; auto-fires once confirmed once.
- **Cooldown after action class** — refuse a repeat of the same `op_class` for X minutes after a successful invocation. Defense against runaway loops.
- **Decision provenance** — extend `GuardDecision` with the chain of rules that fired (currently we only return the *first* refusing rule's reason). Better audit trail.

Each is a new guard slotted into `guarded_act`'s composer. Add the guard, add the env var, add the test, document here.

## Related

- AEOS extension spec — see `spec-aeos-0.1.md` §"Agent action safety" (cross-link).
- Memory: `feedback_repo_hygiene_proactive.md` — the broader "proactively prevent runaway agent behavior" thread.
- Issues: [#218](https://github.com/b-tree-labs/axiom-os/issues/218) (rate limit), [#219](https://github.com/b-tree-labs/axiom-os/issues/219) (pause verb).
