# ADR-285: RIVET decides on content and earns its autonomy from a record

**Status:** Proposed (2026-11-24). It becomes Accepted when chunk 1 and chunk 2 of `docs/prds/prd-rivet.md` land with their proofs passing.
**Related:** ADR-269 (Ship, Show, Ask tiers), ADR-278 (credentials have one holder), ADR-257 D8 (commit attestations), `docs/prds/prd-rivet.md`; issues #2147 (umbrella), #2305, #2347, #2366, #2307, #2301, #2300, #2378.

## Context

RIVET lands pull requests, watches main, reverts a red main and carries out releases. Today it decides by which files a change touches and by whether checks are green. It keeps no common record of its decisions, so there is no evidence on which to give it more freedom or take freedom away. Its own identity token failed for days with no alarm, a `warn` mode still landed pull requests, and a run with no tests read as passing.

The industry study of 2026-11-24 (`docs/working/rivet-industry-study-2026-11-24/`) found that the merge-queue products decide on green checks and approvals alone, that forge-native agents fix failures and keep a PR mergeable but decide nothing about whether it should land, and that no product keeps a durable decision record or lets autonomy grow from a measured track record. It also found public cases of agents hijacked through pull request text and of a bot auto-merging a compromised dependency within an hour. Most fix rates are vendor-reported; no independent benchmark exists.

## Decision (proposed, to be proven)

- **Decide on content, fail closed.** A change is classified by what it does (a migration that is not expand-only, a destructive tool, a deletion, a workflow) and not only by path. A PR takes the least-autonomous tier of anything it touches. A risk that cannot be determined, a check that did not run, or an identity that is missing means Ask. Nothing lands on "no failures" alone: it needs a named count of tests run.
- **Decisions are history, not memory.** The decision record is owned and served as history by CLERK (ADR-282). RIVET's recall of past incidents may explain a note; it never decides a tier. Anything stored from pull request text is stored as derived facts only (signature, counts, links), because stored text is a path for instructions into a later run.
- **One decision record.** Every landing, hold, refresh, revert, tag and release writes one record to one ledger schema: the inputs, the policy version, the tier, the decider, the outcome and any later revert. The record is append-only, exportable and replayable over past PRs. A release record is signed with the identity that signs the artifacts.
- **Autonomy is earned and reversible.** A path class moves from Ask to Show to Ship only on measured agreement with people and a measured revert rate, after a shadow period in which RIVET records what it would have done. A revert demotes the class. Changes to the policy itself are Ask.
- **Pull request text is data.** Titles, bodies, comments, branch names and failing-test output never carry instructions. A fixer run holds no secret beyond its own short-lived, repo-scoped token, and may not remove or weaken the change under test, edit the tests it must pass, or leave auto-merge armed while it pushes. A fix carries a fail-before, pass-after receipt.
- **No required external dependency.** RIVET needs git, the forge's basic API (pull requests, checks, merge) and a model reached through the owner's setting. Everything else is an optional adapter that RIVET degrades without. A new external component must be well vetted, genuinely open source (an OSI license, no relicense risk) and substantial. A hosted vendor feature, a forge's own merge queue included, is never a requirement. RIVET keeps its own thin landing queue (test the merged state, ordered removal with reason codes, a cost-labelled hotfix lane); batching and bisection are added only if measured cost justifies them (#2399).
- **Gating components are replaceable.** Where a decision needs a statistical graduation gate, the requirement is written in Axiom's spec and an outside component, Postrule among them, may satisfy it. Axiom builds no such gate.
- **A change carries a manifest, and RIVET follows it past publish.** The manifest names the flag and default, the ring plan and watch window, the reversal that applies, a plain user-visible statement, the usage events to watch and the feedback route. RIVET orchestrates deploy to the idle slot, the swap, the ring ramp and the reversal, and reads usage and feedback as signals. It does not own slots, flags, update or feedback; each keeps its owner (ADR-170, ADR-179, ADR-257).
- **A host contract.** RIVET runs on any host that meets the written contract in #2378, hosted or self-hosted, with no Axiom-only import in the landing path.

## Consequences

- C1 (safety rails) delivers the fail-closed and identity parts; it retires the `warn`-lands behaviour and the empty-check-is-green path.
- C2 (main and CI truth) delivers the shared ledger schema first, so shadow data starts accumulating before anything depends on it.
- C4 (risk by content) retires path-only classification for destructive tools and migrations.
- C5 (release train) delivers the signed release record and post-publish verification.
- C6 (earned autonomy) delivers the promotion and demotion rules and the first Ship pilot.
- C5 also delivers the lifecycle spine (#2385): the change manifest, a reversal choice rule, a changelog composed as the user-visible delta between two versions, one version map, and release quality and engagement measured from the record.
- C3 also delivers the thin landing queue (#2399).
- #2305, #2347, #2366 and #2304 stop describing private ledgers and use the shared schema.

## Proof

- Fail closed: a PR with no tests run is held with the exact fix named; a missing identity holds with a loud alert; `mode = "warn"` lands nothing.
- Record: replaying 60 days of PRs through the ledger reproduces each historical tier decision and lists the ones that differ.
- Earned autonomy: a seeded revert demotes its path class within one run, and the demotion is in the ledger.
- Hostile text: a fixture PR whose title, body, comment and branch name each carry an instruction produces no change in RIVET's behaviour and no secret in any output.
- Receipt: a fix that deletes the change under test, or edits the failing test, is refused.
- Lifecycle: a sandbox change with a manifest ramps through its rings unwatched; a seeded bad signal in ring one turns the flag off and the record says why; `axi update --check` shows only the changes that apply to the person; a seeded stale pin fails the version map check.
- Release: killing a sandbox release mid-way resumes at the right step; the record verifies offline with the same tool as the artifacts.
