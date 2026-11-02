# How to declare requires, produces and next in your skill

A guide for anyone adding or sweeping a skill. The decision behind it is
[ADR-185](../adrs/adr-185-tools-declare-what-they-need-and-make-and-vetted-chains-are-published-as-recipes.md);
the contract is `docs/specs/spec-aeos-0.1.md` section 4.9.7, and the standard
that says which level of this an extension claims is section 4.11 (AEOS 0.2 draft,
[ADR-186](../adrs/adr-186-chain-readiness-is-a-level-an-extension-claims-and-lint-verifies.md)).

The idea in one line: **a skill works alone, says what it needs, says what it
makes, and when a need is unmet it refuses with the exact fix.** It never imports
another skill. Everything below is a field on `SkillSpec`, all optional, and a
skill that sets none runs exactly as before.

## The minimum every skill owes

```python
SkillSpec(
    name="report.build",
    fn=build,
    description="...",
    side_effects=True,
    risk="reversible-write",      # read | reversible-write | destructive
    requires=(),                  # not a read? say so, even if the answer is "nothing"
)
```

- `risk` is required on every skill. Pick the worst thing a mistaken call does:
  `read` changes nothing; `reversible-write` changes something you can put back;
  `destructive` removes or replaces something you cannot.
- `requires` is required on every skill that is not `read`. An explicit `()` is an
  answer ("I need nothing"); leaving it out is not.

In this repository the debt lint (`tests/infra/test_skill_chain_lint.py`) fails on a
new skill that omits either. Do not add a new skill to
`tests/infra/skill_chaining_debt.txt`; that list only shrinks. For any other
extension the same two fields are what chain level B asks for (see the migration
note below), and `axi ext lint` checks them.

## requires: say what must be true, and how to make it true

```python
Precondition(
    "source-exists",
    "the source file must exist",
    check=lambda params, ctx: Path(params.get("source", "")).exists(),
    fix="create it, or pass source=<path>; list candidates with 'axi report sources'",
)
```

- `check(params, ctx)` runs before your function, on every call. Keep it **cheap
  and side-effect free**: look at a file, a setting, an index. Never open a
  credential, write, or hit the network. It returns `True`/`False`, or
  `(False, "why")` to say more. A check that raises counts as unmet.
- `fix` is the **exact** command or setting. It may name parameters as `{source}`;
  they are filled from the call. A fix that says "check your configuration" is a
  wall, not a fix; the constructor rejects an empty one.
- A precondition with no `check` is documentation: it is listed in the refusal
  catalogue and `recipe.plan`, but the runtime cannot test it.
- To require something a *previous* skill produced, use `consumes`:

```python
Precondition("snapshot", "a snapshot id is needed", consumes="snapshot_id",
             param="id", fix="call state.snapshot and pass its snapshot_id as id")
```

With no `check`, the runtime tests that `id` is present and non-empty, and
`recipe.check` verifies that a recipe wires a `snapshot_id` ref into it.

What the caller sees when a check fails (your function does not run):

```json
{"ok": false, "errors": ["report.build did not run: source-exists: ... Fix: ..."],
 "chain": {"v": 1, "unmet": [{"id": "source-exists", "why": "...", "fix": "..."}]}}
```

## produces: name the references your result carries

```python
produces=(Ref("snapshot_id", "pass as id to state.restore or state.show"),)
```

Put the value in `SkillResult.value` under the same key (or set `Ref(..., field="id")`
to name a different key). The wrapper lifts it into `chain.produced` with your
`how_to_use`. Only scalars (strings, numbers, booleans) are lifted, and a ref is an
**identifier, never a secret value**.

A produced kind must be consumed by something (a `consumes`, a `feeds`, a recipe
input) or documented: its `how_to_use` must name a skill or a backticked command.
The lint flags an orphan.

## next_steps: say what is worth doing after

```python
next_steps=(
    NextStep("state.show", "produced:snapshot_id", "inspect what was captured",
             feeds={"id": "snapshot_id"}),
    NextStep("state.prune", "ok", "drop old snapshots when you are done"),
)
```

- `target` is a registered skill (the lint checks it resolves) or a command line.
- `when` is `ok`, `error`, `always`, or `produced:<kind>` (the kind must be in your
  `produces`).
- `feeds` fills the target's input from a produced ref, so the result carries the
  argument ready to use: `{"skill": "state.show", "with": {"id": "snap-1"}}`.
- Offered only if it exists and is exposed on the caller's surface. A CLI-only skill
  is never offered to an MCP client, and a slash command never to a protocol client.
- A skill can also return dynamic follow-ups: append mappings with `skill` (or
  `command`) and `why` to `SkillResult.next`. They are validated the same way.

## destructive skills: plan, then apply

Mark it `risk="destructive"` and you are done. From an MCP client, a chat turn or a
browser, the first call opens a plan and returns a `plan_hash`; a person approves it
(`axi approve ok <id>`); the second call carries `plan_hash` and the **same other
parameters** and runs once. You do not write any of that. Your function never sees
`plan_hash`. The CLI is not gated, so your verb still needs its own confirmation
(`--yes`, or a typed name), as `secrets.rm` has.

If you are writing a verb an agent must be able to reach in an emergency, think twice
before `destructive`: it puts a person in the path. The leak closer is
`reversible-write` for that reason (ADR-155).

## recipes: be a step of one

If your skill is a step of a published recipe, list the recipe id in `recipes=(...)`.
The lint requires it in both directions. To add a recipe, add it to
`axiom/infra/recipe_catalog.py`; to name a skill that does not exist yet, list it in
the recipe's `placeholders` (and its `expects` ref kinds on the step). `recipe.check`
reports each placeholder as missing and fails when one has appeared.

## Calling a chain

Pass `_chain = {"chain_id": "<one id per run>", "parent_step": "<step_id of the
previous result>"}` on each call. It is stripped before validation, and the ids are
written to the action audit, so the sequence can be reconstructed.

## Checklist

1. `risk` set; `requires` set (or `()`), on anything not a read.
2. Each `fix` is a command someone can paste.
3. Each `check` is cheap and has no side effect.
4. Each produced kind is consumed or documented.
5. Each `next_steps` target resolves and is exposed where you expect.
6. Delete your skills from `tests/infra/skill_chaining_debt.txt`, then run
   `python scripts/skill_chaining_debt.py` to regenerate the report.
7. Run `pytest tests/infra/test_skill_chain_lint.py`.
8. If an extension is now clean, claim it: `aeos_version = "0.2.0"` and
   `chain_level = "B"` in its manifest (`tests/infra/test_chain_level_claims_ratchet.py`
   fails until you do).

## Declaring it in the manifest (chain level B and C)

The `SkillSpec` is the source of truth. For `axi ext lint`, and for any consumer that
must not import your code, the same facts are written to the skill's
`[[extension.provides]]` table, which `axi skills emit` generates. A `check` is code
and is not copied; the table carries `id`, `description`, `fix` and the
`consumes` / `param` wiring.

```toml
[[extension.provides]]
kind = "skill"
name = "report.build"
entry = "report.skills.impl:build"
path = "report/skills/build"
risk = "reversible-write"
side_effects = true
requires = [{ id = "source-exists", description = "the source file must exist", fix = "pass source=<path>" }]
produces = [{ kind = "report_id", how_to_use = "pass as id to report.show" }]
next_steps = [{ target = "report.show", when = "produced:report_id", why = "inspect it", feeds = { id = "report_id" } }]
```

`requires = []` is an answer ("I need nothing"). Leaving the key out means "has not
said", and level B reads the difference.

## Migration note for extension authors: what to add, in what order

You do not have to do anything to keep conforming: an extension that declares
nothing is Level A. To claim more, one level at a time:

1. **Set the claim.** In `[extension]` set `aeos_version = "0.2.0"` and
   `chain_level = "B"`. Run `axi ext lint`. It will fail with AEOS102 and list what
   is missing. (Or leave the claim at A and run `axi ext lint --chain-level B` to see
   the same list without failing.)
2. **Add `risk` to every skill** (AEOS110). The finding says which skill and gives
   the line to add; choose `read` only if it changes nothing, `destructive` only if
   a mistaken call cannot be undone.
3. **Add `requires` to every skill that is not a read** (AEOS111), `requires = []`
   when nothing must hold first. Each entry needs a `fix` you could paste into a
   terminal (AEOS113).
4. **Make produced refs useful** (AEOS114): name what to do with each, or let a
   later skill consume it.
5. **Make follow-ups resolve** (AEOS115, AEOS116): targets are real skills, and a
   `feeds` or `produced:` kind is one the skill produces.
6. **Regenerate the table** from the code with `axi skills emit` if your skills are
   registered in code (AEOS130 fails if the two differ).
7. **For level C**, add a recipe (`axi ext init` writes a stub to copy): a data file,
   a `kind = "recipe"` block, each step a real skill, each destructive step with
   `approval = true`, and the recipe id in each step skill's `recipes` (AEOS120 to
   AEOS127). Then set `chain_level = "C"`.

At every step `axi ext lint` prints, per skill, the rule id, what is wrong, and the
exact fix. To check behaviour as well as declarations, call the checks in
`axiom.infra.chain_conformance` from your own tests: they run your skill through the
real dispatch path and report any of AEOS140, 141, 142 and 150 that it breaks.

A new extension needs none of this by hand: `axi ext init` scaffolds a Level B
extension with a declared skill and a recipe stub.
