# PRD: The loop: build, deploy, improve without a pull request

**Status:** Draft (2026-11-12). **Decision:** [ADR-257](../adrs/adr-257-a-change-reaches-main-through-the-loop-not-a-pull-request.md).
**Extends:** ADR-210 (feature flags), ADR-254 (one switchboard, proposed), ADR-170 and ADR-182 (release slots, never down), ADR-197 (the switch contract), ADR-142 (attestation), ADR-045 (earned graduation).
**Related:** ADR-109, ADR-169, ADR-236, ADR-238, ADR-250 (proposed), ADR-253.

## 1. Problem

Every change to Axiom reaches main through a pull request. The pull request does five
jobs at once: it proves the change works, it limits who is exposed to a bad change, it
is where a person judges a risky change, it records who approved what, and it is the
only thing that stops an agent from writing to main. It does none of them well.

- **It proves too late and too slowly.** The checks run after the work is finished, on
  a branch that is already behind main, and run again after every rebase.
- **It does not limit exposure at all.** Once merged, a change is on for everyone who
  takes the next release. The only way to narrow it is to not merge.
- **It queues work behind one person.** Review is the scarce step, so finished work
  waits, branches drift, and conflicts are paid for twice.
- **It finds nothing after release.** A pull request closes when the change merges.
  What users and agents then experience has no path back to the change that caused it.

The owner's goal, stated 2026-11-12: with test-driven development, fuller smoke test
automation, blue/green deploys, feature flags with rollout cohorts, and feedback
channels for users and agents, pull requests can be eliminated in favour of a tighter,
continual build, deploy and improve loop.

## 2. Evidence

- **A merge freeze on main (2026-11-11).** Every merge cancelled the main run in
  progress, so no main commit finished its checks and a release could not be tagged.
  All sessions stopped merging for hours. A workflow merged one pull request during the
  freeze because the freeze was a message between people, not a lock.
- **Main went red several times in one week** from changes that had passed their own
  pull request checks, because the branch was tested against an older main.
- **The hosted CI budget was exhausted in two days (2026-10-29 to 10-30)** by rebase
  and update-branch runs: the same checks, repeated, on changes that had not changed.
- **Nine pull requests from one work stream were open at once on 2026-11-12**, two of
  them conflicting with main only because they waited.
- **The flag system cannot narrow a release.** A flag resolves through a fixed ladder
  (default, environment, variant, node, account, user). There are no cohorts, no
  percentage rollout, no kill switch and no way to ask why a flag is on for someone.

## 3. Goals and non-goals

**Goals**

1. A change reaches main in minutes, already proven, and is off for everyone until a
   flag turns it on.
2. Exposure is a decision made after the change is on main: by cohort, by percentage,
   reversible at once.
3. A bad change is stopped without a deploy (kill switch) or removed without a person
   (automatic revert, slot rollback).
4. What users and agents report reaches the change and the flag that caused it.
5. A person still judges the changes a flag cannot contain, and that judgement is a
   signed record, not a button on a pull request.
6. The loop is earned by tier. No tier loses its pull request until its gates exist
   and have held.

**Non-goals**

- Removing human judgement from shared core, migrations or authorization.
- A hosted flag service or a third-party flag product.
- Changing how a published release is attested (ADR-109 stands).

## 4. Users

- **Contributor (a person or their coding agent).** Writes the test, writes the code
  behind a flag, pushes. Wants to know in minutes that it is on main and safe.
- **Operator of a node.** Turns a flag on for a cohort, widens it, or stops it. Wants
  to see who is exposed and why, and to stop a flag from any surface.
- **Owner of shared core.** Signs the changes that need a person. Wants to see only
  those.
- **User of an app built on the platform.** Reports that something is wrong or right
  in one action, from where they are.
- **An agent acting on a node.** Reports a failed or surprising capability call the
  same way a user does.

## 5. Requirements

Each requirement has an ID so a test can carry it (acceptance harness).

### 5.1 Cohorts and rollout (LOOP-F)

- **F1. A cohort is a named, stored set.** Members are an explicit list of principals
  or accounts, or a rule over attributes, or both. Membership is "listed, or the rule
  is true".
- **F2. Rules compose.** `and`, `or`, `not`; an attribute test (`eq`, `neq`, `in`,
  `not_in`, `contains`, `gt`, `gte`, `lt`, `lte`, `exists`); and "is in cohort X". Depth
  and size are limited at save time; a cycle is refused with its path named.
- **F3. Attributes come from a catalogue.** The platform declares the common ones: role,
  site, account, account kind, node role, environment, principal kind (person, agent),
  locale. An extension declares more in its manifest, each with a resolver. An
  attribute with no data resolves **unknown**, and unknown never admits a member, even
  through `not`.
- **F4. A rollout is an ordered list of rules per flag and environment.** Each rule
  names a cohort or an inline match, a value, and optionally a percentage and a time
  window. The first rule that decides wins.
- **F5. Percentage assignment is stable.** The bucket is a hash of the flag key and the
  subject, so a subject stays in or out across sessions and nodes, and different flags
  do not start with the same subjects.
- **F6. A kill switch per flag and environment beats everything**, including user and
  account overrides, and takes effect on the next request with no deploy. Setting it
  needs a lower permission than editing a rollout, so an incident can be stopped fast.
- **F7. One resolution order, reported with every answer:** kill switch, user
  override, account override, rollout rules, node, variant, environment, default. The
  extension-off rule (ADR-254) sits above all of them. Each resolved value carries what
  decided it.
- **F8. Explain.** For a flag and a subject, the platform returns the value, what
  decided it, the rule and cohort matched, the bucket, any override that was ignored
  and why, and any attribute that was unknown.
- **F9. Writes are previewed.** A cohort or rollout write returns what would change and
  who would be affected (counts and a sample) and applies only through the plan a
  person approved (the platform's existing plan hash for destructive tools). Every
  write is audited with its before and after.
- **F10. Overrides can carry a reason and an expiry.** A flag protected in an
  environment accepts only a reasoned override there. An expired override is ignored
  and reported.
- **F11. Every flag has an owner and a review date.** A flag fully on past its review
  date, or off and unreferenced, is reported to its owner as work to remove.
- **F12. One evaluation, on the server, on every surface** (CLI, MCP, HTTP, in-process,
  and the web app's slice). The client receives resolved values only.
- **F13. Cohort rules live only in the record (the database).** The node file keeps
  only the switches a node needs before its database is up (ADR-254). A node with no
  database resolves flags from the registry, the environment and the node file, and
  says so.

### 5.1b Every standard rollout mode (LOOP-F)

The owner, 2026-11-12: rollouts are managed mostly at cohort scale, from tens of agents
to whole geographies or time zones, with explicit small hand-picked sets as a rarer
mode; the platform supports every mode of flagging and rollout that is standard in
industry. F1 to F13 give rules, cohorts, percentages, windows and the kill switch.
These complete the set.

- **F14. Bucket by any attribute.** A percentage rollout chooses its unit: a principal
  (default), an account, a site, a node, a time zone, a cohort, or any catalogue
  attribute. Everyone who shares the unit's value is in or out together, so a rollout
  can move whole sites, regions or groups of agents at a time.
- **F15. Geography and time are attributes.** Time zone, country and region of a
  principal (from their profile) and of a site and node (from site configuration),
  plus local time of day and day of week for the subject, so a rollout can say "sites
  in this region" or "only during local working hours".
- **F16. Large explicit sets.** A cohort's list is imported in bulk (a file, or a
  directory group by reference), kept in the record not the flag, and reports its size.
  A cohort can follow an external group (an identity provider group or a federation
  cohort) and refresh from it on a schedule.
- **F17. Multivariate flags.** A flag may serve values other than on, off and soon: a
  string, a number or a document from a declared set of variants. A rollout rule
  serves one variant or splits a unit across variants by weight.
- **F18. Progressive rollout.** A rollout may be a plan of stages (for example 1, 5,
  25, 50 and 100 percent, or ring by ring: internal, a canary cohort, early sites,
  everyone), each with a minimum soak time and health gates. The platform advances a
  stage only when its gates hold and halts or rolls back by itself when a gate fails
  (errors, refused calls, feedback reports from LOOP-B, or any declared metric).
- **F19. Scheduled changes.** Any rollout change can be scheduled for a time, in a
  stated time zone, and is previewed and approved like an immediate one.
- **F20. Prerequisites.** A flag may require another flag to resolve to a given value
  for the same subject; explain shows the chain.
- **F21. Experiments.** A flag may be run as an experiment: variants assigned by
  stable bucketing, a holdout group, mutually exclusive layers so one subject is in at
  most one experiment of a layer, exposure recorded once per subject, and results per
  variant on declared metrics with their uncertainty (through the platform's
  uncertainty machinery, never a bare number).
- **F22. Flag kinds.** Each flag declares its kind: release, operational, permission,
  experiment or kill switch. The kind sets its defaults (a release flag must have a
  removal date; a kill switch defaults on and is exempt from staleness).
- **F23. Approvals by environment.** Who must approve a change to a cohort, rollout or
  stage plan is configurable per environment and per flag, through the platform's one
  permission model; production can require two people.
- **F24. Live updates to clients.** A connected client (the web app, an agent runtime)
  learns of a change within seconds without polling the whole set, and a kill switch
  reaches it at once.
- **F25. A standard provider interface.** The platform's evaluation is exposed through
  an OpenFeature-compatible provider, so code in any language can read flags the
  standard way and the platform can be swapped in or out like any flag service.

### 5.1c The shell and the rail (LOOP-F)

The owner, 2026-11-12: rollout changes what the shell and the rail show in the
platform's base web app and in every product built on it. Today the rail hides an item
whose flag resolves `off` and marks one that resolves `soon`; the web UI library reads
the caller's resolved map from `/api/v1/flags`, assumes three values, and re-reads it
only when the page regains focus. Requirements:

- **F26. The rail follows the caller's resolution.** Cohorts, percentages, schedules,
  prerequisites and the extension-off rule are resolved on the server per caller, so
  two people on one node may see different rails. The shell never evaluates a rule.
- **F27. No surprise mid-session.** A rollout or schedule that changes a rail item
  while a person is using it takes effect at their next page load, or when a time
  limit passes for a page left open (a setting; default 4 hours), so a console open
  for a whole shift does not run old behaviour indefinitely. Never under their cursor.
  A kill switch is the exception: it removes or disables the item at once, and the
  page they are on says why. (Owner, 2026-11-12.)
- **F28. Live enough for a kill switch.** The shell learns of a kill switch within
  seconds (F24), not only when the window regains focus.
- **F29. More than three values.** The web UI library accepts multivariate flags (F17):
  the rail reads on, off and soon; a component reads its variant; an unknown value
  never breaks the rail.
- **F30. One Settings home for switches.** The switchboard's three kinds, cohorts,
  rollouts and stage plans, the kill switch, and approvals live under Settings, in the
  one design system, with each row showing its value and what decided it. A rollout's
  view shows who is exposed and the feedback from them (B6).
- **F31. "Why do I see this?"** Any rail item or flagged surface can explain itself to
  the person in front of it (F8), in plain words, limited to what they may know.
- **F32. One path, one name.** The web UI library moves from `/api/v1/flags` to
  `/api/v1/features` before the alias ends (ADR-254), in the platform's base app and in
  every product's app.

The screens for F30 and F31 are designed in the master design before they are built
(the design contract).

**Who manages rollouts, and where (owner, 2026-11-12).** Managing feature flags across a
fleet of sites is a console's job, like a cloud provider's console, and the console comes
later. Until then, a site's own flags are managed in the regular app by its **site
admin**: Settings has a Site admin group, shown only to that role, with Feature flags
(each flag's rollout, stages and the people exposed), Cohorts and Node (what runs, and
preferences); a rollout or cohort change is previewed and signed there. Everyone else's
Settings holds only their own preferences. Every person gets the rail behaviour (F26 to
F29), "Why do I see this?" (F31, the full trace only for a site admin) and the stopped
page. The word "switch" is never shown on screen. On the command line, `axi features`
covers what a node runs and `axi flags` covers feature flags, over the one registry
(ADR-254). The role formerly called `operator` is `site-admin`. A site's flag record lives
on its site node and a console coordinates sites (ADR-260).

### 5.2 Proof before and after main (LOOP-S)

- **S1. Test first.** A change arrives with the test that fails without it. This is
  already the standing default for contributors and their agents.
- **S2. The push is the gate.** The pre-push check runs lint, the content guards and the
  affected tests against current main, and refuses a push when main is red.
- **S3. Content guards run before the push.** Anything that cannot be taken back once
  it leaves the machine (forbidden terms on the public surface, names the platform
  must not carry, secrets) is checked locally, and again on the server before the
  commit is accepted.
- **S4. Main is checked on every commit, and a breaking commit is reverted
  automatically.** The revert names the commit, the failing check and the author, and
  opens an ask for the author. A known flake is re-run once before it counts.
- **S5. A smoke suite of user journeys runs against a release in an idle slot** before
  traffic moves (ADR-170, ADR-169), and again after. A failed journey keeps or
  restores the previous slot (ADR-197).
- **S6. A release is cut only from a main commit whose smoke suite passed**, and the
  release lock is a lock every merging workflow obeys, not a message.
- **S7. One check per tree; no duplicated tests or per-commit jobs; good-enough
  assurance plus fast detection and rollback** (owner, 2026-11-12). CI and CD efficiency
  is a driving requirement. Each behaviour is proven by one test, and a new test proves a
  behaviour no existing test proves. A new per-commit CI job is not added; a check that
  must run on every commit joins the job that already runs on that tree. A change says
  what each new check catches that nothing else does. Where proof before main would be
  slow or costly, fast detection (S4, LOOP-B) and rollback (F6, B7, ADR-197) are
  preferred to exhaustive proof.

### 5.3 Feedback (LOOP-B)

- **B1. One action to report**, from the web app, the CLI, chat and MCP: good, bad, or
  a sentence. No form.
- **B2. A report carries its context without the reporter typing it:** the surface, the
  release, the node, the flags that resolved on for that subject, and the capability
  call or screen it came from.
- **B3. An agent reports the same way.** A capability call that fails, is refused, or
  returns something the agent could not use becomes a report with the same context.
- **B4. Reports group by cause.** Many reports with the same release, flag and
  capability are one item, counted, not many.
- **B5. A grouped report reaches the owner of the flag or capability** as an ask in
  "Needs you" (ADR-253), obeying what that person has said may reach them (ADR-238).
- **B6. A flag's rollout view shows its reports**, so widening a cohort is decided with
  the reports from the cohort already exposed.
- **B7. Roll back by default** (owner, 2026-11-12). When a bug is reported, the default
  is to roll back. The owner of the implicated flag (F11; until a flag has one, the
  site's site admins) or of the release gets the grouped report in Needs you with the
  rollback already chosen: "Stop this flag" when a flag is implicated, "Roll back the
  slot" when the release is. "Fix forward instead" needs a reason. A report marked
  severe, or the same cause reported by several distinct reporters (a site setting;
  default 3 within 1 hour), stops the flag automatically through the kill switch (F6)
  and tells the owner; its reason reads "auto: N reports of <cause>" and the public
  message is the standard line. A slot is never rolled back from feedback alone: it is
  offered. Every automatic stop is audited and raises one notification. Reporting is
  one action with no form, reports are grouped by cause and never listed one per
  report, and only the owner sees the ask.

### 5.4 Judgement and record (LOOP-J)

- **J1. Tiers.** Tier 0: documents, tests, design assets. Tier 1: extension and feature
  code that a flag can contain. Tier 2: what a flag cannot contain (schema migrations,
  authorization, secrets, packaging and pins, CI workflows and hooks, the guards, the
  flag system itself, and the protected paths).
- **J2. A tier 2 change needs a person's signature.** The signature is an attestation
  (ADR-142) whose subject is the commit. Software cannot sign. When it is required
  depends on whether a revert undoes the change (owner, 2026-11-12):
  - **Before the push** for what a revert cannot undo or what reaches outside: schema
    migrations, authorization, secrets, release and publishing, CI workflows and hooks,
    and the guards. The server refuses an unsigned commit that touches these.
  - **After, within a window,** for the rest of tier 2 (for example the flag system's own
    code). The change lands, the platform asks its owner to sign, and an unsigned change
    is reverted automatically when the window closes. The window is a setting per
    repository; default 24 hours.
- **J3. An agent pushes to main only under a grant** that names the repository, the
  tier and an expiry, issued by a person (ADR-250, proposed). Without one, an agent's
  change arrives as a branch.
- **J4. Graduation is earned and can be lost** (ADR-045): a tier moves to direct push
  after its gates have held for a number of clean changes, and returns to pull
  requests after a number of reverts within a window. All three numbers are settings,
  per repository and per tier. Defaults (owner, 2026-11-12): 10 clean changes to
  graduate; 2 reverts within 14 days to return. A change to these settings is audited
  like any other settings change.

## 6. Success metrics

Each is a named quantity with its unit; targets are proposals for the owner.

- **Time from push to on main**, median, in minutes. Target: under 15.
- **Open pull requests per repository**, count at end of day. Target: zero for
  graduated tiers.
- **Minutes main is red per week.** Target: under 30, with every red ended by an
  automatic revert.
- **CI minutes per landed change**, median, in minutes, hosted CI and the pre-push gate
  counted apart and reported per tree (S7). Target: hosted minutes half of the October
  2026 figure, and no rise when a requirement is added.
- **Time to stop a bad flag**, from first report to kill switch, in minutes. Target:
  under 5.
- **Share of reports that carry a flag and a release.** Target: 100 percent.
- **Flags past their review date**, count. Target: zero older than 30 days.

## 7. Phased delivery plan

Each phase ships behind its own flags and is usable alone.

1. **Foundation.** The switchboard (ADR-254) lands. The release lock, head-run truth
   and flake re-run land (already written, awaiting landing).
2. **Cohorts and kill switch (F1 to F10, F12, F13).** Ported from a rule engine already
   proven in another product built on the platform, with that product's attributes
   left behind. Explain and preview on every surface.
2b. **Every standard rollout mode (F14 to F25).** Bucket by any unit, geography and
   time, large and synced sets, multivariate flags, progressive rollout with health
   gates, scheduled changes, prerequisites, experiments, flag kinds, approvals by
   environment, live updates, and the standard provider interface.
2c. **The shell and the rail (F26 to F32).** Designed first in the master design's
   Settings and shell sections, then built in the web UI library and adopted by the
   platform's base app and each product's app.
3. **Automatic revert and server-side guards (S2 to S4).** Tier 0 graduates to direct
   push when this phase has held.
4. **Smoke journeys in the idle slot (S5, S6)** and flag lifecycle reporting (F11).
5. **Feedback (B1 to B7).**
6. **Commit attestation and agent grants (J2, J3).** Tier 1 graduates after phases 2
   to 5 have held. Tier 2 moves from pull request to signed commit.

## 8. Risks and open questions

**Risks**

- **A flag hides unfinished code that still runs.** Import-time and migration-time
  effects are not behind the flag. Mitigation: those paths are tier 2 by definition.
- **Flag debt.** Mitigation: F11, and a flag is created with its removal date.
- **Automatic revert fights a flake.** Mitigation: one re-run, and a revert of a revert
  needs a person.
- **Installers from the package index take a release, not a slot.** Mitigation: S6, and
  new behaviour defaults off in the registry.
- **Direct push removes the forge's only guard against an agent.** Mitigation: J3 is a
  precondition for agents, not a follow-up; until it exists, agents use branches.

**Decided (owner, 2026-11-12): the order repositories move.** Axiom first; then the web
UI library (it hosts the flag and rollout screens); then the product repository (it has
outside contributors, so the gates are exercised by people other than the platform's
authors); then the site repositories, which move only after the product repository has
held tiers 0 and 1 for the graduation count, because a site repository deploys to a
node people depend on. Repositories of other organizations adopt the loop by their own
choice.

## 9. Links

- ADR-257 (this decision), ADR-254 (one switchboard), ADR-210 and
  `docs/specs/spec-flags.md` (flags today).
- ADR-170, ADR-182, ADR-197, `docs/specs/spec-release-slots.md` (slots and switching).
- ADR-169, `docs/specs/spec-synthetic-journeys.md`, `docs/guides/acceptance-harness.md`.
- ADR-142, `docs/specs/spec-attestation.md`; ADR-109 (build provenance).
- ADR-045, ADR-236 (graduation and permissions); ADR-238, ADR-253 (asks).
- `docs/conventions/local-run.md` (run it locally).
