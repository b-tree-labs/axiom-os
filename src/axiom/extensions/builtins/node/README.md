# node — KEEL, the one agent for the machine a node runs on

**ADR-230.** One agent owns the machine: what runs on it, how it is doing,
what is wrong and the fix, where its configuration has drifted, and what can
be reclaimed. It absorbs TIDY (`hygiene`), TRIAGE (`diagnostics`) and the
`install_health` doctor. The ship is its figure
([docs/conventions/the-ship.md](../../../../../docs/conventions/the-ship.md)):
KEEL is the structure that keeps the vessel upright; "on an even keel" is the
state it reports.

## The noun and its verbs

| verb | answers | reads / changes |
|---|---|---|
| `axi node inventory` | what runs here: engines (detected, and declared when Axiom did not install them), the models they serve, systemd units, listening ports, GPU use, disks by tier | reads |
| `axi node vitals` | how it is doing right now: free space per mount against its floor, memory, load, GPU; a verdict | reads; `--alert` hands a breach to HERALD |
| `axi node doctor` | what is wrong and the exact fix: the platform's health checks, including the local-model alignment check | reads |
| `axi node drift` | where it disagrees with itself or its source: pins vs ensure unit vs serving env; live files vs tracked source; hand edits with no source | reads |
| `axi node report` | the node in one document, for handover and onboarding | reads |
| `axi node declare engine\|tier\|tracked` | tell KEEL what the machine cannot tell it | writes the node's declarations; recorded |
| `axi node clean` | what could be reclaimed, as a plan | plans; applying is a separate approved step; nothing measured is deleted |

Every verb is a skill (`node.<verb>`), reachable from chat, MCP and the
command line alike, with `--json` everywhere. There are no sub-nouns: `declare`
takes a kind. Approvals ride the platform's Ask / Auto dial, so there are no
propose / approve / deny verbs.

## Hand-offs

KEEL is one crew member among several. The skills are written so the
hand-offs are real calls or declared follow-ups, not prose.

| From KEEL to | when | how |
|---|---|---|
| **HERALD** (`notifications.alert`) | a vitals breach on the heartbeat, a drift finding that matters | `node.vitals --alert` invokes it; the heartbeat is `node vitals` every 15 minutes |
| **KEEP** (`vault.exposed`, `vault.rotate`, `vault.where`) | an engine's credential sits on its command line (a process listing is an exposure) or in an env file | `inventory` and `drift` report `credential_exposed` with the next step |
| **RIVET** / `update.check` | an installed package is behind its release | `doctor` carries the finding; the follow-up is `update.check` |
| **PLINTH** (`data.storage`, `data.backup`) | disk pressure on the tier that holds the database | `vitals` names `data.storage` as the next step |
| **fleet** (`fleet.report`) | the report is what the console carries | `report` names `fleet.report` as the next step |
| **GUARD** / the Ask dial | `clean --apply`, `declare` | mutating skills; the registry gates them |

| To KEEL from | when | call |
|---|---|---|
| **PLINTH** before a heavy job (adopt, compress, backup) | needs headroom and the right tier | `node.vitals` (pressure per mount), `node.inventory` (disks by tier) |
| **RIVET** before and after a deploy | never deploy onto a drifted node; confirm what runs after | `node.doctor`, `node.drift`, then `node.inventory` |
| **WARDEN** / the fleet console | what a node is before trusting it | `node.report` |
| **AXI** (chat) | any question about this machine | every skill, through the registry |
| the install's own doctor | `axi doctor` | its checks become `node.doctor`; the command stays |

## Declarations

`<state>/node/declarations.toml`, written by `node declare` (or by hand):

```toml
[[engines]]
name = "qwen-vllm"
kind = "vllm"
unit = "vllm-server.service"
port = 41883
weights = "/home/<llm-account>/vllm/.hf"
owner = "llm-account"

[[tiers]]
mount = "/home"
tier = "hot"
floor_gb = 150

[[tiers]]
mount = "/bulk"
tier = "bulk"

[[tracked]]
live = "~/landing-conf/default.conf"
source = "serving/frontdoor/default.conf"
```

A declared engine is reported even when its process is not seen (as "declared,
not running"); a detected engine with no declaration is reported with the
follow-up to declare it. A tier's floor is what `vitals` judges against; a
mount with no declared tier is judged at 10 % free.

## Conformance

AEOS 0.2 manifest, chain level B; a Steward and a Sensor (AEOS 1.0 §3.11).
No in-process state: everything is read from the machine or the declarations
on each call (§2.13). Declarations are written with the platform's atomic
state write and every change is returned as an action (§2.14's dedicated
primitive is not built yet; this is the durable, auditable path available).
Destructive steward operations are not offered (§6.2): `clean` archives,
never deletes measured data. `axi ext lint` passes; `axi skills emit-md --ext
node` generates each skill's `SKILL.md` from its `SkillSpec`.

## Status

0.1.0: `inventory`, `vitals`, `doctor`, `drift`, `report`, `declare`, and
`clean --plan`. Next: `clean --apply` for archive moves under the Ask dial;
the old extensions deleted once their remaining skills have moved (ADR-230
order of work).
