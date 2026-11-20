# ADR-263: Documents and agents are shared by Google Drive's roles, an agent adds Invoker, and an agent run acts with the lesser of two authorities

**Status:** Accepted (2026-11-13, by the owner; points 12 to 14 added the same day). The questions in
[prd-sharing-and-access.md](../prds/prd-sharing-and-access.md) §9 are decided as recommended there.
**Related:** [ADR-209](adr-209-every-installation-is-a-site.md) (D2 visibility, D7 one authorization model),
[ADR-205](adr-205-a-key-belongs-to-a-person-and-what-it-reads-is-resolved-from-grants-and-sharing-programs.md) (grants and silos),
[ADR-153](adr-153-a-role-is-granted-at-a-site.md) (a role is granted at a site; autonomy composes by minimum),
[ADR-261](adr-261-the-governing-role-is-site-admin-and-admin-reads-as-access-admin.md) (`site-admin`, "Access admin"),
[ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md) (rule 8: administering grants no authority in a book),
[ADR-208](adr-208-conforming-utilities-are-governed-catalog-objects.md) (`user#delegate@agent`),
[ADR-213](adr-213-upload-is-a-composable-extraction-pipeline.md) (the Data Book),
[ADR-237](adr-237-every-agent-runs-on-one-harness-an-agent-is-a-contract.md) (an agent is data),
[ADR-045](adr-045-raci-evolution.md) (approval tiers). ADR-250 (proposed, branch only: one grant noun, the baton attenuates).
**PRD:** [prd-sharing-and-access.md](../prds/prd-sharing-and-access.md). **Design:** appkit `docs/design/proposed-2026-11-13/sharing/`.

## Context

A person signed in sees their account, but nothing tells them which documents and agents are theirs,
which were shared with them, by whom, and what they may do with each. The Library has two
visibilities (`site`, `private`) and one ownership check (`scope.can_change`: the uploader). The check
is applied unevenly: `service.move_item` and `book.service.create_folder`, `change_folder` and
`delete_folder` skip it, so a colleague at the same site can move another person's file, delete their
folder, or rename a scheme folder for the whole site (issue #1952). "Shared with me" lists
colleagues' site files, because person-to-person grants do not exist (`views.shared_with_me`,
`basis: "site"`).

Agents have one accountable person (`AgentRecord.accountable`; `agents.assign` designed, #1930) and no
notion of who else may see, change or use them. A scheduled fire runs with no principal at all:
`schedule/executor.py` builds a `SkillContext` with none, so a skill falls back to the node's own
handle (`library.scope.caller_from_skill` uses `local_handle()`). Today an automation acts with the
machine's authority, which is wider than any person's.

The owner asked for one model, Google's, applied exactly to documents and to agents, with one more
privilege for agents because they act. GUARD already has the shape this needs: `decide()` writes a
receipt for every decision, and the OpenFGA substrate's starter model is the owner, editor, viewer
chain Google's own authorization system uses.

## Decision

**A document, a folder and an agent each have one Owner and an access list of people and groups with
Google Drive's roles (Viewer, Commenter, Editor), plus a general access line (Restricted, or Everyone
at the site with a role). An agent adds one role, Invoker, between Commenter and Editor. Every verb
names the least role it needs, and one decision point in GUARD answers for every surface. An agent run
acts with the lesser of the agent's own authority and that of the person it works for.**

1. **Roles.** Owner, Editor, Commenter, Viewer, with Google's meanings: a Viewer opens, previews,
   downloads and copies; a Commenter also comments; an Editor also changes, files and shares, and an Editor of a
   folder may move what it holds to Deleted files; the Owner also deletes for good, transfers
   ownership and sets the item's sharing settings. Sharing changes only who may do each Library
   act: every action and interaction of the original Library stays (the Library parity inventory). Roles are a ladder:
   a higher role holds every right of a lower one. Exactly one Owner, always a person.
2. **General access.** `Restricted` (only the people listed) or `Everyone at <site>` with one role.
   There is no "Anyone with the link", no sharing outside the item's site, and no organization-wide
   line (ADR-205 D4, ADR-209 D9: a site is a silo; public is discovery only).
3. **Settings, as Google's.** "Editors can change permissions and share" (on by default; the Owner
   may turn it off). "Viewers and commenters can download and copy" (on by default; the Owner may
   turn it off). Access may expire, on any date within a year; KEEP owns the expiry (ADR-209 D8a).
4. **Folders inherit; the Book does not.** A file in a person's folder has at least the access its
   folder gives (Google's rule: never less than its parent). The Data Book's scheme categories are
   filing places, not shared places: filing a file there never changes who can see it (LIB-R1.6). The
   classifier files automatically, and a placement that shared a file would let a classifier decide
   access. The Book's structure has one shared-drive role, **Book manager**, who may rename or move
   a scheme category for the site; site-admins hold it by default.
5. **Agents.** The same roles and dialog, and **Invoker**: may give the agent work (talk to it,
   mention it, give it a task, start it now, add an automation to its schedule, and change, pause or
   remove the automations they added). Editor includes Invoker. The Owner is the person who answers
   for the agent (ADR-237; `agents.assign` transfers it, and the new person must accept). Everyone at
   the site sees an agent's Team row (name, handle, mark, one sentence, who answers for it, paused or
   retired), because Team answers "who and what is here" (DC-19); everything else needs a role.
6. **Whose authority an agent run uses.** The effective authority of an agent run is the
   intersection of (a) what the agent may do (its capabilities and Ceiling, set by its Owner) and
   (b) the authority of the person it works for: the person who started it, or, for an automation,
   the person who added it, re-checked at every fire. An automation declared by an extension works
   for the agent's accountable person. A child run receives at most its parent's (ADR-250 D2). No
   agent run ever reads or does what the person it works for could not.
7. **Administration is not authority over content** (the Library's reading of ADR-142 rule 8). A
   site-admin sets the site's sharing policy, holds Book manager, may pause or retire any agent, and,
   through the offboarding step only, transfers a departing person's documents and agents to a named
   person. A site-admin cannot open a Restricted document, change a document or an agent's
   instructions, or give themselves a role on either. Every such act is recorded and the Owner told.
8. **An export-control hold and a clearance stay above every share.** A share never lets a person
   read a file their clearance does not cover or a file held for review; while a file is held, its
   shares are suspended (kept, not used) and new shares are refused with the reason.
9. **One decision point.** `axiom.governance.access` answers `role_of(principal, object)` and
   `require(principal, verb, object)` from one verb table (each skill names its least role). It is
   a policy source in GUARD's combiner, so every answer is a `decide()` receipt. Each skill calls it,
   so the CLI, MCP, chat and the web get the same answer (DC-16). No role at all reads as not found;
   a role too low reads as refused, with the role needed and the request path (R8).
10. **Storage is expand-only.** Three tables in the `authz` extension's schema: access grants
    (object, subject, role, expiry, who gave it, how, revoked), access settings (general access, its
    role, the two Google settings) and access requests (asked role, note, state, decision, reason).
    The Owner stays where it is today (`library_items.owner`, `library_folders.created_by`, the
    agent's accountable person). `library_items.visibility` is written in step as a projection until
    every reader has moved. When the OpenFGA substrate is on, each grant is also written as a tuple
    on new types `document`, `folder` and `agent` with the relations `owner`, `editor`, `invoker`,
    `commenter`, `viewer` (ADR-209 D7 extended); the tables stay the durable record.
11. **Migration keeps every access a person has today.** A `site` file becomes Everyone at the site
    · Viewer; a `private` file becomes Restricted; each uploader is its Owner. A person's folder
    becomes Restricted (its files keep their own access; only the folder's name leaves other
    people's tree). An agent keeps its accountable person as Owner and gets Everyone at the site ·
    Invoker, which is what everyone can do with it today. What #1952 let people do (move another
    person's file, delete or rename another person's folder, rename a scheme category without being
    Book manager) stops, and the release note says so.
12. **An agent's access is a limit, never extra access.** When a person creates or edits an agent
    they set its Access: Files (entries over the Library tree from Home, each Use or Use and change)
    and Connectors (which ones, with which capabilities). The default is everything shared with the
    site, and anything shared wider, plus the agent's own folder (Home › Agents › <name>); a person's
    own Restricted files only when an entry names them. A run reaches the intersection of the access,
    what the person it works for can open, and the agent's capabilities. The folder a person is in
    when they ask is attached to that request only; there is no persistent current folder.
    Cloning an agent offers to copy its access (on by default, the owner's decision); only what the
    person cloning could have added by hand is copied, never a person's own connector, and the rest
    is listed by name.
13. **The sandbox is compiled from the access and is never shown.** For each agent run the node
    compiles one run reach (`agents/pipeline/sandbox.py` `StepReach`) from the access, the person and
    the step. Library items are reached through skills and the decision point only; the storage root
    is never mounted. A tool that needs real files gets `in/` (read-only, only items that passed the
    check, through the content door), `out/` (uploaded on finish into the agent's folder through the
    full pipeline) and `scratch/` (deleted at the end). There is no network; connector calls go
    through the node, which holds the credential. A step whose class this host cannot enforce is
    refused, never run unconfined.
14. **Connectors are set up once and granted.** A site connector is a site-admin's, a personal
    connector a person's own (ADR-238 D1, D5); neither is configured inside an agent. A site
    connector is shared with the same roles (Invoker = may use). A run uses a site connector's own
    credential when the agent lists it and its person may use it, and a personal connector only for
    that person's own runs. Capabilities on a connector are the intersection of the agent's list,
    the person's right and the profile's `capability_scope` (ADR-110).

## Options considered

- **Keep two visibilities and add owner checks to the five verbs that skip them.** Fixes #1952 and
  nothing else: no person-to-person sharing, no "Shared with me" that means it, no way to say what a
  person may do with an agent. Loses the owner's requirement.
- **Map the Data Book to a Google shared drive** (membership grants access to everything filed in
  it, with Manager, Content manager, Contributor, Commenter, Viewer). Lost: the Book is filled by a
  classifier, so membership-by-placement would let the classifier decide who reads a file, and it
  contradicts LIB-R1.6. Only the Manager role is borrowed, as Book manager.
- **The agent's privilege as a checkbox on any role** ("can give it work"). Truer to Google Cloud,
  whose `roles/run.invoker` is separate from edit; lost because it doubles the dialog's questions,
  and an Editor without it could still schedule work by editing the agent. Kept as an owner
  question.
- **An agent run acts as the agent's Owner** (Apps Script's "Execute as me"). Lost: anyone given
  Invoker borrows the Owner's whole access, the confused-deputy case. **As the starter only:** lost,
  because the Owner's limits on the agent (its capabilities, its Ceiling) would not hold.
- **An agent that sees nothing until folders are added.** Lost: an agent that can do nothing on
  day one is the default nobody keeps, and with the lesser-of rule a broad default shows no one
  anything new. **An agent that sees everything its person can open, private files included:** lost,
  because it puts every person's private files in reach of any agent they talk to, the place a
  mistake or an injected instruction costs most.
- **Connectors configured per agent.** Lost: a credential copied into each agent, a reconnect per
  agent, and no single answer to who may use a service.
- **A per-agent current folder.** Lost: hidden state on a shared agent, so one person's run acts in
  the folder the last person left it in.
- **Mounting the Library's storage for file tools.** Lost: a second path around the decision point,
  labels and clearance; materialising through the content door keeps one.
- **OpenFGA as the only store.** Lost for now: the substrate is off by default (`NullSubstrate`) and
  an outage abstains, so access to a file would depend on an optional service. Tuples are the mirror.

## Consequences

- Easier: one dialog, one list of people with access, one decision point, one record for every share;
  #1952 closes as a consequence of the verb table rather than five patches; "Shared with me" means it.
- Harder: retrieval can no longer rely on the corpus alone (a site file is in the site corpus, but a
  Restricted file shared with three people is in none of theirs), so library search and chat
  retrieval filter by the decision point for directly shared items; `ScheduleDefinition` gains the
  person an automation works for, and the executor builds a principal for every fire.
- The sandbox becomes real work: a compiler from access to reach, an enforcer for `read_only` and
  `ephemeral_container` (Phase 2 of `sandbox.py`), materialisation through the content door, a
  run-scoped connector door, and confinement on macOS and Windows (ADR-036 D10 says there is none).
  Until each exists, the decision point enforces files for skills, and steps that need the OS are
  refused.
- Committed to: an Owner is always a person; a run never holds a credential or general network; an agent never acts beyond the person it works for;
  administration never opens content; an export-control hold outranks every share; no anonymous or
  cross-site sharing without a new ADR.
- Follow-up: the build plan in the PRD §10; ADR-250's grant noun lists shares as a fifth kind,
  `share` (amend ADR-250 before it is accepted); ADR-209 D7 gains three types; the Library PRD's
  decisions 2 and 4 (#1965) are answered by this one.
