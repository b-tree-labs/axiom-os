# Product Requirements (One-Page)

**Product / Feature:** Any data element, from a harness — MCP, CLI, and the UI that points at them

**Owner:** Ben Booth   •   **Status:** Approved — building P1 first (Ben, 2026-09-30)   •   **Last updated:** 2026-09-30

---

## 1) Elevator Pitch

Anything a person can see on a chart, an agent can fetch — by MCP tool, by
`axi data`, in the format it will be used in — and the surface says so at the
moment somebody reaches for the download button.

## 2) Problem / Opportunity

A reader who wants the numbers presses **Export** and gets a CSV of the window
on screen. That is the whole path. Everything else — a script, a notebook, a
harness, an agent — has to be told about the platform by a person.

**What actually exists today, read from the extension on 2026-09-30:**

| | State |
|---|---|
| Gold read verbs | `data.catalog`, `describe`, `series`, `aggregate`, `roles`, `compare` — as skills, so MCP gets them |
| Tier access control | **Already right.** `_tiers()` defaults to `public`; a caller cannot widen its own tiers by passing a parameter, and an explicit `tiers` is honoured only for an assured principal |
| On the CLI | **Nothing.** `axi data` offers `install, diagnose, ingest, unregister, list`. No gold verb is reachable from a shell |
| Formats | **None.** No CSV, NDJSON, Parquet or Arrow anywhere in the extension. Results are skill JSON |
| Bronze / silver reads | **No verb at all**, for anyone |
| Anything in the UI that says agents can do this | **Nothing** |

So the guard is built and the door is not. The honest summary is that we have a
good access model, one transport, and no formats.

## 3) Goals & Success Metrics

- **Primary goal:** a researcher writes a five-line script against our data
  without asking anybody how.
- Success metrics:
  - **`axi data series --format csv > day.csv` works** from a shell with no
    Python, no DSN, and no knowledge of the schema.
  - An agent already holding our MCP can answer "give me the fuel temperature
    for 2026-09-10" **without a human naming a table**.
  - **Every Export in the UI shows the same thing as a command**, copyable.

## 4) Key Users / Personas

- **Researcher with a harness.** Claude Code or a notebook. Wants a dataframe,
  not a download. Will not read an API reference first.
- **Script author.** Wants a cron job that pulls yesterday's gold and writes a
  file. Cares about exit codes and formats.
- **Operator at the UI.** Does not know an agent could do this and has no reason
  to guess.
- **Partner site.** Same verbs, their own data, and nothing of anybody else's.

## 5) Scope — Key Capabilities (MVP)

### C1 — The gold verbs reach the CLI

`axi data tables | describe | series | aggregate | roles | compare`, the same
six that already exist as skills, wired to the same implementations. One
implementation, three faces — the CLI is a face, never a second query path.

*Acceptance:* every gold skill has a CLI verb, asserted by a test that walks the
manifest rather than a list somebody maintains by hand.

### C2 — Formats

`--format json|csv|ndjson|parquet`. JSON is what a skill already returns; CSV is
what a spreadsheet takes; NDJSON is what a stream of records should be; Parquet
is what a dataframe wants and what stops people converting CSV badly.

**Units travel with the values.** A CSV whose header says `value` and not
`value_degC` is the bug this programme spent a day on. Every format carries the
unit, the site, the channel and the window — in the header for CSV, in the field
names for NDJSON, in the schema metadata for Parquet.

*Acceptance:* a CSV export and the chart it came from name the same units, and a
channel with no declared unit exports as a refusal rather than as a bare number.

### C3 — Size, and why there is no stream

There is no streaming transport. A verb answers or refuses.

`--limit` with a default, `--since/--until`, and a refusal that says how to
narrow when a request would be too large — the same shape as the figure lane's
"nothing numeric in that window to draw". Paging by window is the answer, and it
is honest: a caller that asks for a year of a 10 Hz channel should be told the
number before it arrives, not fed it slowly.

*Open:* if somebody genuinely needs a year, the answer is probably a signed URL
to an object the platform wrote once, not a socket held open.

### C4 — Bronze and silver are readable by default *(revised, Ben 2026-09-30)*

**I had this wrong.** The first draft put bronze and silver behind an assured
principal, on the reasoning that they are not contracts. Ben's correction: a
person writing a bronze→silver conform extension **cannot do the work without
reading both**, and that person is an ordinary developer, not a database
administrator.

So the default for a user with no super-admin or DB-admin right is: **read
bronze, read silver, read gold.** Gold is what they build ON; bronze and silver
are what they check their work against.

**The tier is not the access boundary.** It never was — it is a *maturity*
boundary, and conflating the two is what produced the first draft. Access is
already row-level: `access_tier` on the row, filtered per query, and — this part
is good and should be kept — **the filter is recorded in the provenance**, so
two callers with different grants get answers whose method strings differ rather
than two different numbers that look identical.

What remains true, and what the response must still say: bronze is what arrived
and silver is mid-derivation, so **both change shape when a conform pass
changes**. A reader of either is reading our implementation, and every response
says so.

**What still needs designing — restriction WITHIN bronze.** Ben: they "might
need to be restricted from some bronze data". Bronze holds whatever arrived,
which can include a partner's data under agreement, or material a site has not
agreed to share. The unit of restriction is therefore the **source**, not the
tier: a connector declares who may read what it deposits, and the conform
toolkit has to let somebody write a transformation for a source they cannot
read — schema without content, shape without values. That is an open design
problem and it is stated as one below.

**An implementation fact that shapes all of this:** bronze is not a SQL tier
today. `FilesystemBronzeSink` writes `<root>/<source>/_records/<date>/<item>.json`
with content-addressed blobs and `_quarantine` / `_excluded` sidecars; the
Iceberg-backed lakehouse sink is later work. So "describe a bronze table" means
describing *deposits* — their source, their day, their decision, their record
shape — and only tabular sources have anything SQL-like. A verb that pretends
otherwise will work in tests and not on a node.

### C4a — Introspection across tiers *(added, Ben 2026-09-30)*

The read verbs take `--tier bronze|silver|gold`, defaulting to gold, so the same
six verbs answer "what is in bronze", "what shape is this silver table", and
"does my conform pass produce what I expected". They reach MCP, the CLI, and
`neut chat` by the one route every capability already takes.

**This collides with ADR-128 and the collision has to be resolved, not
finessed.** `tiers.py` codifies `SERVING_TIER = "gold"` and `WORKING_TIERS =
("bronze", "silver")`, and defines serving as *"anything whose output reaches a
person, an agent, an API, a figure or a report — a count for a status line is
serving, and so is a chart's provenance string."* By that wording, an agent
reading bronze over MCP **is** serving and is forbidden.

The proposed amendment, for the walkthrough: **serving is about relying, not
about reading.** The rule exists so that nobody answers a question about the
world from untransformed data. A developer inspecting their own conform work is
not answering a question from it; they are checking a transformation. So:

- `SERVING_TIER = gold` stands, unchanged, for anything that **answers**.
- A separate, explicit, **labelled** introspection surface reads the working
  tiers, and every response it produces carries that label — so a chat reply
  built on silver cannot be mistaken for an answer, because the label travels
  with it the way the tier filter already travels in the provenance.

That keeps the reasoning that made ADR-128 worth having, rather than eroding it
to fit a convenience.

### C5 — The UI points at the agentic path

Beside Export, a control that shows **the same figure's data, as a command**:

```
axi data series --site andretti-reactor --feed shadow.crh \
  --channel measured_cm --from 2026-04-03 --to 2026-04-10 --format csv
```

and the MCP call that does the same thing, with a line for anybody who has not
connected the MCP yet. Built from the figure on screen, so it is never an
example — it is *this* chart, as a command.

This is the piece that changes behaviour. Somebody who has pressed Export three
times is the person most likely to want a script, and that is the moment to say
it is possible.

*Acceptance:* the command shown, pasted into a shell, returns the data the
figure drew.

## 6) Non-Functional / Constraints

- **One implementation per verb.** A CLI that queried directly would drift from
  the skill within a month, and the tier guard lives in the skill.
- **The guard is not a parameter.** `_tiers()` already refuses to widen on
  request; nothing added here may weaken that, and the CLI is not a way around
  it.
- **A refusal says what to do next.** "Too large" without a narrower suggestion
  is a dead end.
- **Nothing exports without a declared unit** — same rule as the chart.
- **Reads are attributable.** A gold read through any face carries its principal,
  the same as every other capability.

## 7) Timeline (high level)

- **P1** — C1 and C2 for gold: CLI verbs and formats. This is the bulk of the
  value and touches no access control. **Prioritised ahead of the dashboard
  work** so it can go to Zavier and his undergraduate — Ben, 2026-09-30.
- **P2** — C5, the UI affordance, once a command exists to show.
- **P3** — C3 limits and refusals, informed by what people actually ask for.
- **P4** — C4/C4a bronze and silver introspection, which needs the ADR-128
  amendment agreed first and the bronze deposit shape settled second.

## 8) Open questions for the walkthrough

1. **Is `axi data series` the right front door**, or should the easy path be a
   single `axi data get` that takes a question and picks the verb? The six verbs
   are precise and none of them is the obvious first thing to type.
2. **Does the UI show one command or the harness's own form?** A Claude Code user
   wants the MCP call; a shell user wants the CLI line. Showing both is honest
   and twice the clutter.
3. **Parquet means a dependency.** `pyarrow` is not small. Optional extra, or in
   the base?
4. **Does an export carry its provenance?** The figure's caption names the
   source, the window and the resolution. A CSV that loses that is a file nobody
   can cite.
5. **What is the largest answer we will give?** C3 has no number in it yet, and
   that number is a policy decision rather than a technical one.

6. **Does the ADR-128 amendment hold?** "Serving is about relying, not reading"
   is a real weakening of a rule that exists because two serving paths once read
   `silver.signals` directly. If the label is the only thing keeping an
   introspection read from becoming an answer, the label has to be enforced
   rather than remembered.

7. **How does somebody conform a source they may not read?** The toolkit needs
   schema without content — field names, types, cardinality, a value's shape but
   not its value. That may be a different verb rather than a flag on this one.

8. **What declares who may read a source's bronze?** The connector registration
   is the obvious place, and nothing there says it today.
