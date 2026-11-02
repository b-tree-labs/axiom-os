# ADR-185: Tools declare what they need and what they make, and vetted chains are published as recipes

**Status:** Accepted (2026-10-08)
**Related:** [ADR-056](adr-056-skill-as-function.md) (skills are the executable unit), [ADR-072](adr-072-capability-projection.md) and [ADR-073](adr-073-mcp-registry-driven-tool-surface.md) (one capability, many projections), [ADR-155](adr-155-an-agent-may-contain-a-leak-before-it-may-manage-a-key.md) (the containment verb stays reachable by an agent), [ADR-157](adr-157-the-serving-tier-bounds-what-any-one-call-can-cost.md) (budgets at one door), `docs/specs/spec-aeos-0.1.md` (§4.9.7), `docs/specs/spec-builtin-mcp-server.md` (§6.6), `docs/guides/skill-chaining-author-guide.md`

## Context

The platform has well over a hundred skills, and each works alone. The value
of a platform is the whole being bigger than the parts, and that depends on a
caller (a person in chat, or an agent in another harness over MCP) knowing which
tool follows which, what a refused call needs, and where a person must say yes.
Today none of that is data. It is prose in a description, or knowledge in
someone's head, and it fails the same way each time: the caller guesses an
order, calls a tool whose precondition is unmet, receives an exception string,
and either gives up or improvises around the refusal.

Four questions forced the decision. How can these tools be chained at all? How
would a chat, or a harness reaching us over MCP, know to chain them? Should a
tool ever depend on another? And what makes the whole larger than the sum?

The tempting answer to the third is yes: let a tool import the tool it follows.
That is the answer this ADR rejects, because it ends the property that makes a
skill safe to expose: it works alone. A skill that imports another skill cannot
be projected, tested, authorized or audited without dragging the other along.

## Decision

**A tool declares what it needs and what it makes; it never imports another
tool. Joining is done by the registry and by published recipes.** Four parts.

1. **Declarations on `SkillSpec`, all optional.** `requires` (named
   preconditions, each with a cheap side-effect-free check and the *exact fix*),
   `produces` (typed refs: kind plus how to use it), `next_steps` (follow-ups:
   target, when, why), `risk` (`read`, `reversible-write` or `destructive`, the
   same vocabulary the chat command registry uses), and `recipes` (membership).
   `None` means "has not said"; an empty tuple means "says there is nothing";
   the lint reads the difference. An undeclared skill behaves exactly as before.

2. **One wrapper at the dispatch chokepoint.** `invoke_capability` (and chat,
   which dispatches beneath it) evaluates the declared preconditions before the
   skill runs and returns a *structured refusal*, `unmet: [{id, why, fix}]`, never
   an exception. After the skill runs it adds one additive, versioned `chain` key
   to the result envelope: `next` (declared follow-ups filtered to what is
   registered and exposed on this surface, plus any the skill returned, with the
   argument already filled in from a produced ref) and `produced`. Existing result
   fields are untouched. A caller may pass a reserved `_chain` argument
   (`chain_id`, `parent_step`); the wrapper mints a `step_id` and writes all three
   into the existing action audit record, so a sequence is reconstructable from
   the ledger we already keep. No new store.

3. **Destructive means plan, then apply.** A `risk="destructive"` skill reached
   from an MCP client, a chat turn or a browser runs only with a `plan_hash` that
   names an *approved plan*. The first call opens the plan on the existing
   approval queue and returns the hash; a person approves it with the existing
   `approve` verb; the apply call carries the hash back. The hash is a digest of
   the capability and its parameters, so an approval cannot be reused for
   different parameters, and the approval is spent (compare-and-set to
   `running`) before the body executes, so it applies once. Knowing the hash is
   worth nothing without the approval record. The CLI and the runner are exempt:
   a person is already at the verb, and the runner *is* the apply half of the
   queue.

4. **Recipes: vetted chains, published.** A `Recipe` is data: an intent sentence,
   ordered steps naming skills, how an earlier step's ref feeds a later step's
   input, an approval point on every destructive step, and rollback hints. It is
   checked against the live registry (`recipe.check`: every step real, refs line
   up from `produces` to `requires`, destructive steps carry approval, membership
   is declared on both sides) and dry-run (`recipe.plan`). It is published as an
   MCP prompt `axiom_recipe__<id>` (described so it cannot be mistaken for a
   harness built-in) and as a line in the capabilities block ("to do X, follow
   recipe R"), but only when every step is a real tool on that surface. **There is
   no runner.** The agent follows the recipe, calling each skill through the
   ordinary chokepoint, so every step keeps its own authorization, audit and
   refusal.

The capabilities block grows `needs` and `makes` hints and the recipe lines, and
is now bounded in bytes (2400), not only lines: hints are dropped first, then the
lowest-priority capability lines; the frame and the recipe lines never give way.

Enforcement is a strict debt list, `tests/infra/skill_chaining_debt.txt`, in the
shape of the names-no-domain guard: it names each skill still owing declarations,
fails if a listed skill is already clean, and fails if any other skill owes
anything, so it only shrinks. `docs/working/skill-chaining-debt.md` is generated
from it, grouped by extension, as the sweep's work list.

## Options considered

- **Tools import the tools they follow.** Rejected, above: it destroys "works
  alone" and with it independent projection, testing and authorization.
- **A workflow engine that runs recipes.** Rejected for this change. A runner would
  have to reimplement authorization, audit and refusal per step or bypass them,
  and the agent following a recipe is already the thing that handles a refusal
  well. A runner can be added later as one more caller of the chokepoint.
- **Put the next step in the description prose.** Rejected: it is what we have, and
  it cannot be validated, filtered by what exists, or filled in with the argument.
- **Gate destructive skills on every surface, the CLI included.** Rejected: the
  CLI skills confirm themselves, and a plan/approve loop in front of a person who
  is already at the terminal is ceremony.
- **Make `secrets.exposed` destructive.** Rejected on purpose (ADR-155): it is the
  containment verb, reachable by an agent because the clock matters more than the
  ceremony, and it records the exposure first and rotates second. It is
  `reversible-write`. `secrets.rm` and `secrets.set` are `destructive`, and are
  CLI-only.

## Consequences

- A refused call tells the caller exactly what to do, in a field a program can
  read. A result tells the caller what is worth doing next, limited to tools that
  exist where the caller is.
- A destructive tool is safe to expose over MCP: it cannot run without a person's
  approval of these exact parameters.
- Every skill now owes declarations. **173 skills across 20 extensions owe them
  today, and 9 extensions have skill functions that are not registered at all**
  (`connect`, `dev`, `fleet`, `identity`, `lane`, `memory`, `principal`, `receipts`,
  `status`). That is the sweep's work list; the lint keeps it honest.
- The wrapper adds a registry lookup, a few cheap checks and one envelope key per
  call; a skill that declares nothing pays only the lookup.
- `vault.declare` was registered unaudited although it writes; it is audited now.
- Recipes ship against placeholders. TODO, owed by the sweep, each reported
  missing by `recipe.check` until it exists (and the check fails if a placeholder
  has since appeared, so the list only shrinks): `process.ls`, `process.stop`,
  `process.panic` (incident-response, credential-leak-response); `state.snapshot`,
  `state.restore` (incident-response); `vault.scrub` (credential-leak-response);
  `chat.renew`, `chat.where`, `chat.compact` (session-handoff); `memory.search`,
  `memory.sources` (memory-verify). The memory skill functions take an injected
  service object rather than serializable parameters, so registering them is a
  design step, not only a declaration.
- Deferred: a recipe runner; per-harness MCP-client conformance of the `_chain`
  argument; usage telemetry for recipes (the block orders them by unused steps, and
  has no measure of recipe use yet); a `plan_hash` approval surface in chat.
