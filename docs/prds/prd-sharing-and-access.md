# Sharing and access PRD: documents and agents

**Owner:** platform (GUARD, library, agents) · **Status:** Draft for the owner's review (2026-11-13) · **Last updated:** 2026-11-13 (agent access, connectors and the sandbox added the same day)

**Decision:** [ADR-263](../adrs/adr-263-documents-and-agents-are-shared-by-drive-roles-an-agent-adds-invoker-and-a-run-acts-with-the-lesser-authority.md) (Accepted).
**Design:** appkit [`docs/design/proposed-2026-11-13/sharing/`](https://github.com/b-tree-labs/axiom-appkit/tree/main/docs/design/proposed-2026-11-13/sharing) (README, prototype, gallery; light, dark, desktop and phone).
**Answers:** the Library PRD draft's decisions 2 (filing and folders, LIB-R4.4) and 4 (Shared with me) on branch `docs/prd-library-draft` (#1965); issue #1952.
**Builds on:** [prd-agents.md](prd-agents.md) "One object, three lenses" (who answers for an agent), [prd-access-and-onboarding.md](prd-access-and-onboarding.md) R8 (no-access screens), [prd-axiom-authz.md](prd-axiom-authz.md) §5.1 (one `decide()`), the design contract DC-10, DC-16, DC-17, DC-19, DC-21, DC-22.

## 1. In one paragraph

Everything a person can open in the Library or in Team is either **theirs**, **shared with them** by
someone, or **shared with everyone at the site**, and the page says which, at a glance. The model is
Google Drive's, word for word: an **Owner**, then **Editors**, **Commenters** and **Viewers**, a
**General access** line, the share dialog with its **People with access** list, **Request access**,
access that expires, and ownership transfer. An agent uses the same roles and the same dialog and adds
one, **Invoker**: may give it work. An agent run never does more than the person it works for could do
themselves. When a person creates or edits an agent they set its **Access** in one sentence (which
files, which connectors), broad by default and narrowed with one switch; the node turns that into
the run's sandbox, which nobody has to see.

## 2. Why

- People cannot tell what is theirs. A colleague's file at the site looks the same as their own until a
  control fails, and some controls do not fail when they should (#1952: moving another person's file,
  deleting their folder and renaming a scheme category for the whole site all succeed).
- "Shared with me" lists everything colleagues filed for the site, because nothing can be shared with
  one person (`library/views.py`, `basis: "site"`).
- An agent has a person who answers for it and no statement of who else may see, change or use it, and
  an automation runs with the node's own authority (`schedule/executor.py` passes no principal; a skill
  then falls back to `local_handle()`).
- The owner's direction, 2026-11-13: follow Google's model exactly; add the one privilege agents need
  because they act; integrate with the permission system; make it obvious.

## 3. As built (2026-11-13, Axiom `main` a420d8128, appkit `main` 20d08ec)

| Piece | Where | What it does today |
|---|---|---|
| Library read rule | `library/scope.py` `can_read` | Same site, and `visibility == "site"` or the caller is the owner |
| Library change rule | `library/scope.py` `can_change` | Same site and the caller is the owner; used by update, label, delete, restore, re-extract |
| Unchecked verbs | `library/service.py` `move_item`; `library/book/service.py` `create_folder`, `change_folder`, `delete_folder` | No owner check (#1952); deleting a folder leaves its subfolders |
| Visibility | `library_items.visibility` (`site`, `private`) | Also picks the retrieval corpus (`rag-org` or `rag-tenant:<site>`, or `rag-personal:<principal>`) |
| Folders | `library_folders` (`site`, `path`, `kind` user or override, `created_by`) | A person's folders and every override of a scheme category are site-wide |
| Shared with me | `library/views.py` `shared_with_me` | Colleagues' site files, labelled `basis: "site"` until grants exist |
| Page | appkit `library/browser/LibraryFileList.tsx` (column "Who can access"), `LibraryBrowser.tsx` ("Share with the site" or "Make private", "Only you", "Everyone at <site>") | Two states; the site shows as an id (appkit #215) |
| Decision point | `authz/decide.py` `decide()` | One verdict and one receipt per decision; rule engine, graduation, optional substrate |
| Relationship model | `authz/fga/site.fga`, starter model; `authz/openfga.py` | `owner → editor → viewer`, `blocked`, `group#member`; substrate off by default (`NullSubstrate`) |
| Roles at a site | `governance/roles.py`; ADR-153, ADR-261 | `reader`, `contributor`, `site-admin` (`*:read`, `*:invoke`, `*:govern`), `admin` shown as "Access admin" |
| Grants | ADR-205 role grants, `governance/capability.py` `CapabilityToken`, attest signing grants; ADR-250 (proposed) unifies them as one `grant` noun with kinds `read`, `act`, `sign`, `run` | No grant on a single document or agent |
| Agent ownership | `agents/roster.py` `AgentRecord.accountable`; prd-agents "One object, three lenses"; `agents.assign` (#1930, not built) | One person answers for an agent; nobody else's relation to it is stated |
| Field visibility | `actors/visibility.py` | Team removes fields on the server by viewer class (member, steward, admin): the pattern this PRD reuses for agents |
| Agent schedules | `schedule/db_models.py` `ScheduleDefinition` | No person recorded; `capability_envelope` minted at registration; fires run with no principal |
| Delegation | ADR-208 `user#delegate@agent`; ADR-250 D2 "the baton attenuates" | A child run receives at most its parent's grant (proposed) |
| Export control | `library/ec_review.py`, `library_items.ec_label`; LIB-R5.2, LIB-R8 | Holds and clearance gate retrieval; a reviewer, never the uploader, releases |
| Administration | ADR-142 rule 8 | Administering the platform grants no authority in a logbook |
| Run reach and sandbox | `agents/pipeline/sandbox.py` (`StepReach`, `classify_reach`: none, read_only, ephemeral_container, vm; `NoSandboxEnforcer` detects only), `agents/sandbox_spawn.py` (a transient systemd unit per dispatch on Linux), `infra/services.py` `sandbox_directives`; ADR-036 D10 | Reach is declared per step by hand; Phase 2 enforcement is not built; macOS has no per-agent confinement |
| Connectors | ADR-057 (the connector primitive), ADR-110 (`ConnectionProfile`: `auth_mode`, `capability_scope`, `credential_ref`), ADR-238 D1 and D5 (a connector is a site admin's or a person's own; one lifecycle), prd-connections FR-007 | "Phase 1: all connections available to all users"; nothing says which agent may use which connector |
| Current folder | none | Axiom has no working directory; a selection is attached to a chat request as a chip (DC-09) |

## 4. Google's model, as Google states it

Taken from Google's help pages (fetched 2026-11-13):
[Share files from Google Drive](https://support.google.com/drive/answer/2494822),
[Make someone else the owner of your file](https://support.google.com/drive/answer/2494892),
[Shared drive access levels](https://support.google.com/a/answer/7337554),
[Search for files](https://support.google.com/drive/answer/2375114),
[Request access (If a file will not open)](https://support.google.com/drive/answer/53355),
[Respond to access requests](https://workspaceupdates.googleblog.com/2023/06/respond-to-access-requests-for-google-workspace-files-efficiently.html).

| Google | What it means | Here |
|---|---|---|
| **Viewer** | "can view the file or folder and open all files within a shared folder" | Copied |
| **Commenter** | "can make comments and suggestions on files" | Copied; offered once comments exist (decision 16) |
| **Editor** | "can open, edit, delete, or move any files within the folder, as well as add new files"; may share by default | Copied: an Editor of a folder may move items in it to Deleted files; delete forever stays the Owner's |
| **Owner** | Transfers ownership; deletes for good | Copied |
| **General access**: Restricted, your organization, Anyone with the link | Who else can open it, each with a role | Restricted and **Everyone at the site** only |
| "Allow editors to change permissions and share" | On by default; the owner turns it off | Copied |
| Viewers and commenters "can download, copy, print" | On by default; the owner turns it off | Copied for download and copy (print cannot be enforced, so it is not offered) |
| Access expiration | A date within a year, per person | Copied; KEEP owns expiry and reminds |
| Notify people | Optional message to the people added | Copied, through HERALD and the person's own settings |
| Folder permissions inherit | "you can't give someone less access to a file than they have to its parent folder" | Copied for a person's folders; **not** for Data Book categories (§5.4) |
| Ownership transfer | Offered to someone already shared with; accept or decline; the old owner becomes Editor | Copied; inside a site no acceptance is needed for a document (as in a work account); an agent's new Owner must accept (prd-agents) |
| Request access | "You need permission" page; the owner is asked; approves with a role | Copied, with R8's confirmation, follow-up and declined states |
| Shared with me; owner filters; `owner:`, `to:`, `from:`, `sharedwith:`, `pendingowner:` | Finding files by relation | Shared with me (direct and group shares only), and an Owner filter: Anyone, Owned by me, Not owned by me |
| Shared drives: Manager, Content manager, Contributor, Commenter, Viewer; files owned by the organization | A team space whose membership grants access to its content | One role borrowed, **Book manager** (structure only); content access is not borrowed (§5.4) |
| "External" badge | Marks files reachable outside the organization | Nothing can be shared outside the site, so there is none |

**Term collisions.** "Owner" is also an administrator role id in `actors/visibility.py`
(`ADMIN_ROLES`) and a relation on the FGA `site` type; "Viewer" is also the FGA `site#viewer` relation
(may read a site's data). The share roles are always on one object (a document, a folder, an agent)
and shown only in the share dialog, its list and an item's access line, so the words stay Google's;
code names them `object_owner`, `editor`, `invoker`, `commenter`, `viewer` on the new FGA types, never
on `site`. "Run" is not used for the agent privilege: the bare word is banned where kinds could be
confused (DC-20), and Axiom already names this right `invoke` (`*:invoke`).

## 5. Requirements: documents and folders

### SHR-R1 Roles and the one Owner

| ID | Requirement |
|---|---|
| SHR-R1.1 | Every Library item and every folder a person made has exactly one Owner, a person. The uploader is the Owner; a copy's Owner is the person who copied it. |
| SHR-R1.2 | Each item and folder has an access list: people and groups (ADR-103 directory groups), each with one role, Editor, Commenter or Viewer, and optionally an expiry date within a year. |
| SHR-R1.3 | Roles are a ladder: Owner > Editor > Commenter > Viewer; a higher role has every right of a lower one. A person's role on an item is the highest of their own entry, their groups' entries, the general access line, and every enclosing folder of a person's (SHR-R4). |
| SHR-R1.4 | Every verb names the least role it needs (the table below), and nothing else decides it. |

| Verb (skill) | Least role |
|---|---|
| See it in a list or search, open, preview, see its versions and its activity (`library.list`, `show`, `search`, preview, `/versions`) | Viewer |
| Download, copy, zip (`/content`, `/copy`, `/zip`) | Viewer, unless the Owner turned download and copy off; then Editor |
| Comment, suggest (when comments exist) | Commenter |
| Rename, change tags or collection, upload a new version, restore an old version, re-extract (`library.update`, `/versions`, `library.reextract`) | Editor |
| **Move it to another folder** (`library.move`) | **Editor of the item**, and Editor of the target when the target is a person's folder (SHR-R4.3) |
| Share it, change a role, remove someone, change general access (`library.share`, new) | Editor while "Editors can change permissions and share" is on; else Owner |
| Delete to Deleted files (`library.delete`) | Owner, or an Editor of the person's folder that holds it (as the reference file browser lets a shared folder's editors delete) |
| Restore (`library.restore`) | The Owner, or the person who deleted it |
| Delete forever (`library.purge`) | Owner only, from Deleted files (unchanged) |
| Confirm or change its export-control label or retention class (`library.label`) | Owner (unchanged) |
| Change its sharing settings, transfer ownership (`library.transfer`, new) | Owner |
| Release or keep held (`library.ec_*`) | The site's reviewer, never its uploader, whatever their share role (unchanged) |

### SHR-R2 General access

| ID | Requirement |
|---|---|
| SHR-R2.1 | Each item and folder has one general access line: **Restricted** ("Only people with access can open it") or **Everyone at <site name>** with one role (Viewer, Commenter or Editor). |
| SHR-R2.2 | There is no "Anyone with the link", no share to a person outside the item's site, and no organization-wide line. A share target must be a person or group granted at the item's site (ADR-153, ADR-205 D4). |
| SHR-R2.3 | A new upload takes the site's default general access (a site setting; the default is Everyone at the site · Viewer, which is today's behaviour; decision 7). The upload dialog shows it and lets the person change it. |
| SHR-R2.4 | Changing general access moves the item's retrieval as today (site corpus in or out), and a direct share makes the item retrievable for the people it names (SHR-R8.3). |

### SHR-R3 The share dialog

| ID | Requirement |
|---|---|
| SHR-R3.1 | One dialog, the same for a document, a folder and an agent, titled "Share “<name>”": an "Add people and groups" field; **People with access** (the Owner first, then each person and group with their role and any expiry); **General access**; a settings control (the two Google settings, for the Owner); **Copy link** and **Done**. |
| SHR-R3.2 | Adding people picks a role, an optional expiry, "Notify people" (on by default) and an optional message. A document share happens at once and says in one line that it was recorded (DC-10: safe to undo). |
| SHR-R3.3 | A person who may not share sees the same dialog read-only: who has access and why they cannot change it ("Only the owner and editors can share", or "The owner has turned off sharing by editors"), with **Ask the owner** (SHR-R6). A Viewer who is not on the list sees only the Owner and the general access line, not the list of other people (as Google). |
| SHR-R3.4 | Copy link copies the item's address in the app. The link grants nothing: whoever opens it is checked like any other request. |
| SHR-R3.5 | Every surface has the same acts: `axi library share <id> --with <who> --role <role> [--expires <date>]`, `--remove`, `--general restricted|site[:role]`, `axi library access <id>` (who has access and why), each on one skill with an MCP tool and a chat ability (DC-16). |

### SHR-R4 Folders and the Data Book

| ID | Requirement |
|---|---|
| SHR-R4.1 | A folder a person makes is theirs (Owner) and shared like an item. An item inside it has at least the folder's access (Google's rule): a share on the folder reaches everything in it, and an item cannot be given less than the folder gives. |
| SHR-R4.2 | The Data Book's scheme categories are filing places. Filing an item in one, by the classifier or by a person, never changes who can see it (LIB-R1.6). The tree shows each person the categories, and only the items they can see. |
| SHR-R4.3 | Moving an item into a person's folder needs Editor on the item and Editor on the folder. When the move widens who can see the item, the person is told before it happens ("Moving it here lets 4 more people see it"), as Google does. |
| SHR-R4.4 | **Book manager** is a site role (granted like any role, ADR-153). Only a Book manager renames, moves or hides a scheme category for the site. Site-admins hold it by default. It gives no access to any file. |
| SHR-R4.5 | A folder's Owner or an Editor deletes it **with what it holds**, as the original and the reference file browser do: the folder, its subfolders and every item in them go to Deleted files as one act, said in one line with **Undo**, and restoring the folder restores all of it. Each item is checked on its own: an item held for export-control review or under a legal hold, or one the person may not delete, stays, and the line says how many stayed and why. A folder that keeps such an item keeps only those. Nothing is left orphaned (today's leftover subfolders, #1952). |
| SHR-R4.6 | Creating a folder needs only being at the site; the new folder is Restricted. |

### SHR-R5 Ownership transfer

| ID | Requirement |
|---|---|
| SHR-R5.1 | The Owner may make anyone already on the access list the Owner. The new Owner is told; the old Owner becomes Editor; the item stays where it is filed. |
| SHR-R5.2 | Transferring a folder does not transfer the items in it (as Google). |
| SHR-R5.3 | When a person leaves a site, the offboarding step (the same one `agents.assign` uses) lists their items, folders and agents and transfers each to a named person, or keeps them under a named person's care; nothing is left without an Owner, and nobody loses access they had through those items. |

### SHR-R6 Request access and the no-access screen (R8)

| ID | Requirement |
|---|---|
| SHR-R6.1 | Opening the address of an item a person cannot see shows "You need access", with **Request access**: the role asked (Viewer by default, Commenter or Editor) and a note. It names no title, Owner or folder: the address alone reveals nothing. |
| SHR-R6.2 | The request is a *Decide* ask in the Owner's Needs you (prd-needs-you 11.4), and in the share dialog's banner, with **Share** (the role chosen, which may be lower than asked) and **Decline** (a reason). Editors who may share may answer too. |
| SHR-R6.3 | The requester sees R8.2 to R8.4: sent, to whom, when; withdraw; one reminder per 24 hours; ask in Chat; and, if declined, by whom, when and the reason as written, with **Request again**. |
| SHR-R6.4 | A person who can see an item but needs more (a Viewer who wants to edit) sees the controls they lack disabled with one line ("You can view this. Ask for edit access") rather than hidden (SHR-R9.4), and the same request path. |
| SHR-R6.5 | A request grants nothing until it is answered, and the answer is the recorded share (SHR-R10). |

### SHR-R7 Expiry

| ID | Requirement |
|---|---|
| SHR-R7.1 | A person's or group's access may end on a date within a year. KEEP owns it: one reminder to the person who gave it a week before, and the entry ends on the day (ADR-209 D8a). |
| SHR-R7.2 | The list shows "until 12 Dec" beside an entry that expires; an expired entry leaves the list and its end is in the item's activity. |

### SHR-R8 Clearance, holds and retrieval

| ID | Requirement |
|---|---|
| SHR-R8.1 | A share never lets anyone read above their clearance, an unlabelled file when labels are required, or a sample (LIB-R5.2). A share to a person whose clearance does not cover the item's confirmed label is refused, naming the label, not the content. |
| SHR-R8.2 | While an item is held for export-control review its shares are suspended (kept, not used) and the dialog says "Sharing waits for the review"; new shares are refused with that reason. A release restores them. |
| SHR-R8.3 | Library search and chat retrieval return exactly the items the decision point says the person may read: general access through the corpus as today, direct and group shares through a filter on the item, never a chunk of an item the person cannot open. |

### SHR-R9 Seeing what is yours

| ID | Requirement |
|---|---|
| SHR-R9.1 | Every row says, without opening it, whether it is the person's own, shared with them, or shared with the site. A new **Owner** column says "me" or the owner's name. The "Who can access" column (as built) reads, for the person's own item, "Only you", "You and 3 others" or "Everyone at <site>"; for someone else's, what the person can do ("You can view", "You can comment", "You can edit"), its icon saying how it reached them (the site, or shared with them). |
| SHR-R9.2 | The list has an **Owner** filter (Anyone, Owned by me, Not owned by me), and **Shared with me** lists only items shared with the person or their groups, newest share first, with who shared it and when. |
| SHR-R9.3 | The item's drawer has an **Access** section: the Owner, "You can view" (or the person's role), "Shared with you by <name>, <date>" when it was, the general access line, and **Manage access** (who may share) or **Ask for edit access**. |
| SHR-R9.4 | A control a person's role does not allow is shown disabled with the reason one step away when it is a thing they would expect to do on this object (rename, move, share, delete), and is absent when it never applies to them (delete forever, transfer, label review); DC-17. |
| SHR-R9.5 | **The Library keeps every action and interaction of the original** (owner's rule, 2026-11-13: Dropbox-style; sharing follows Google; never lose functionality). Every row of the Library parity inventory (appkit `docs/design/library-parity.md`, draft #223) that the original or the build has stays; sharing changes only who may do each. In particular the list keeps Name, Owner (new), Who can access, Modified and Size, row checkboxes and select all, the row star, the row menu and the bulk bar (L1, S1, S3, F15, S7), and columns drop only by pane width, as built. |
| SHR-R9.6 | **One item surface, two sizes.** An item opens in one component: a **quick view** beside the list (the list stays usable) that **expands to full screen** and back; ‹ › and ←/→ step through the files in the list; Esc closes. It shows the preview, then Download, Share, Star and More (Copy link, Make a copy, Version history, Rename, Move, and Delete for whoever may delete), then Access, Details (type, size, modified, where it is filed, uploaded by, came in through, tags, checksum), Versions and Activity. Entered from **Details** it opens on Details. **What happened to it** (the pipeline stages) is one step inside (P1, P2, S5, F14, F16, F18, F25; appkit #214). |

### SHR-R10 Record

| ID | Requirement |
|---|---|
| SHR-R10.1 | Every share, removal, role change, general access change, settings change, transfer, expiry, request and answer is a `decide()` receipt and an entry in the item's activity, in words ("B. Lead gave C. Planner edit access, 14:15"). |
| SHR-R10.2 | `axi grant list` shows shares as grants of kind `share` (ADR-250's noun, amended), and `axi grant revoke` ends one. |

## 6. Requirements: agents

### SHR-R11 Roles on an agent

| ID | Requirement |
|---|---|
| SHR-R11.1 | An agent has the document roles and one more, **Invoker**, between Commenter and Editor: Owner > Editor > Invoker > Commenter > Viewer (decision 3). |
| SHR-R11.2 | The Owner is the person who answers for the agent (prd-agents). `agents.assign` is the transfer; the new Owner must accept, because answering for an agent is a duty, not a convenience (a deliberate difference from a document). |
| SHR-R11.3 | Everyone at the site sees the agent's **Team row**: name, handle, mark, one sentence, who answers for it, and whether it is paused or retired. Team answers "who and what is here" (DC-19); every other field needs a role. Fields are removed on the server (as `actors/visibility.py` does), never blanked in the page. |

| What | Least role |
|---|---|
| Team row (above) | Everyone at the site |
| What it does (its goal and instructions), what it may do (capabilities by plain name), when it asks a person (Ceiling), its other dials, its automations ("Coming up"), its agent runs (title, state, time), its cost this month and its cost ceiling | Viewer |
| An agent run's details and results | Viewer of the agent, **and** read access to each result (a result is a Library item with its own access); the run's conversation is the starter's |
| Comment on an agent run or a result; suggest a change to its Tune (the Owner or an Editor accepts) | Commenter |
| **Give it work:** talk to it or @-mention it (decision 5), give it a task, start an automation now, add an automation, change, pause or remove the automations one added | Invoker |
| Change its name and look, description, instructions, tools and skills and capabilities (only those the editor may grant, never beyond its type), dials, what it remembers; change, pause or remove anyone's automation on it (the person who added it is told); pause the agent; share it | Editor |
| Make its Ceiling stricter | Editor; **raise** the Ceiling (signed): Owner |
| Transfer (`agents.assign`), retire, delete one that never ran, its sharing settings | Owner |

### SHR-R12 Whose authority an agent run acts with (decision 1)

| ID | Requirement |
|---|---|
| SHR-R12.1 | An agent run's authority is the **intersection** of what the agent may do (its capabilities and Ceiling) and the authority of the person it works for. |
| SHR-R12.2 | The person it works for: whoever started it (a message, a task, Start now); for an automation, the person who added it; for an automation an extension declares, the agent's Owner. The schedule records that person (`ScheduleDefinition` gains it). |
| SHR-R12.3 | At every fire the automation is checked again: if that person no longer has Invoker on the agent, has left the site, or lacks what the step needs, it does not fire, and one ask goes to the agent's Owner (move it to someone, or remove it). It never falls back to the node's own authority. |
| SHR-R12.4 | A run started by another run, or work an agent asks another agent to do, receives at most its parent's authority (ADR-250 D2). |
| SHR-R12.5 | A file an agent run makes is owned by the person it worked for and filed in the agent's own folder (SHR-R14.8); the agent's Files view lists the files the viewer can read. |
| SHR-R12.6 | The activity row of every agent run names the person it worked for ("for C. Planner"); the record carries both principals. |

### SHR-R13 Sharing an agent

| ID | Requirement |
|---|---|
| SHR-R13.1 | An agent is shared in the same dialog as a document, with the same list and general access line (Restricted, or Everyone at the site with Viewer, Commenter, Invoker or Editor). |
| SHR-R13.2 | Giving someone Invoker or Editor grants standing authority, so the dialog's **Share** opens the one confirm card (DC-10) with what will be recorded; Viewer and Commenter happen at once. |
| SHR-R13.3 | An agent may be given Invoker on another agent (so one can ask another for work), never Editor or Owner; an agent never shares onward (decision 12). |
| SHR-R13.4 | A person made agent's default general access is Restricted; an agent an extension declares is Everyone at the site · Invoker, which is what everyone can do with it today (decision 7). |
| SHR-R13.5 | The Team list of agents has the Owner filter (Anyone, I answer for, Not mine) and a "Shared with me" view, and each row shows the viewer's role when it is not Owner. |
| SHR-R13.6 | `axi agents share <handle> --with <who> --role <role>`, `axi agents access <handle>`, MCP tools and chat abilities on the same skills (DC-16). |

### SHR-R14 An agent's access: its files (the owner's steer, 2026-11-13)

When a person creates or edits an agent, one section, **Access**, says what it may use: **Files**
and **Connectors**. Access is a **limit**, never extra access: an agent run reaches only what is
both in the agent's access **and** open to the person it works for (SHR-R12), with the agent's
capabilities on top. Adding a folder to an agent's access gives nobody anything.

| ID | Requirement |
|---|---|
| SHR-R14.1 | The Library is shown as one tree with a root called **Home**: My files (the person's own folders), Shared with me, the Data Book (the site's categories), and Agents (each agent's own folder). The tree is a view over the Library's records; nothing is a real directory a person or a tool can open (decision 15). |
| SHR-R14.2 | An agent's file access is a list of entries, each a scope and a right (**Use** = read, or **Use and change**): the site-wide entry **Everything shared with <site>** (every item whose general access is the site, and anything shared wider than the site where such a thing exists); a folder or a file picked from the tree; and **Its own folder** (Home › Agents › <name>), always present, always Use and change. |
| SHR-R14.3 | **The default** for a new agent is the site-wide entry (Use) plus its own folder (Use and change). It does not include a person's own Restricted files: those are reached only when an entry names them, and then only for that person's runs (decision 2). |
| SHR-R14.4 | **Broad or narrow is one switch:** "Everything shared with the site" on or off; then "plus" or "only" the folders picked. Picking is a tree picker from Home with search, where each row shows who can open it; a folder brings everything under it. |
| SHR-R14.5 | The section reads as one sentence, written from the entries: "Everything shared with Harbor Lab, plus Data › 2026 (can change). It saves what it makes in its own folder." On the agent's drawer and Team row that sentence is the whole of it (DC-17). |
| SHR-R14.6 | A viewer of the agent sees, in the sentence, only the entries they can open; the rest are counted ("and 1 folder you cannot open"). Access names never leak through the agent. |
| SHR-R14.7 | Who may set it: an Editor of the agent, and only with entries the Editor can open; an Owner the same. A change is recorded on the agent's activity in words. Narrowing applies at once to runs not yet started; widening is a change of what the agent may do, so it opens the confirm card (DC-10). |
| SHR-R14.8 | A file an agent run makes is filed in the agent's own folder, owned by the person the run worked for, marked "Made by <agent>", with the site's default general access (decision 8). Appkit #201's "Made by an agent" view lists these across agents. |
| SHR-R14.9 | Changing a file needs three things: the entry says Use and change, the person the run works for is an Editor of the file, and the agent's capabilities include the Library's change verbs. Otherwise the run may only make a new file in its own folder. |

### SHR-R14a Copying an agent's access when it is cloned

When a new agent is made from an existing one ("Make another like this", the approved create flow:
Choose, Change, Check, Confirm), the **Change** step carries its access.

| ID | Requirement |
|---|---|
| SHR-R14a.1 | The Change step shows the source agent's access sentence and one checkbox, **Also copy its access**, **on by default** (decided by the owner, 2026-11-13: fine-tuned access is a reason to clone). Unticked, the new agent starts with the default access (SHR-R14.3). |
| SHR-R14a.2 | What is copied (recommended rule): the site-wide entry and its setting; each folder and file entry the person cloning can open, with its right; each site connector the person may use, with its capabilities; each "the requester's own <kind>" slot, because a slot names nobody. The source's own folder is not copied: the new agent gets its own. |
| SHR-R14a.3 | What is never copied: an entry for a folder or file the person cannot open; a site connector the person may not use; any one person's own connector or credential (an agent never holds one). |
| SHR-R14a.4 | Everything not copied is listed **by name** in the step, each with why and how to ask: "Not copied: Shared mail, which only B. Lead can grant. Ask B. Lead". A name the person cannot open is not shown: it is counted ("and 1 folder you cannot open"). |
| SHR-R14a.5 | Copying never widens what anyone can reach: access stays a limit, and every copied entry is one the person could have added by hand. The Check step reads back the new agent's access sentence; creating it is recorded with what was copied and what was not. |

### SHR-R15 The folder you are in when you ask

| ID | Requirement |
|---|---|
| SHR-R15.1 | When a person asks an agent from inside a Library folder (✦ Discuss on it, or the chat opened from it), that folder is **attached to the request** as a removable chip (DC-09), the way a selection is today. The run looks there first and, when asked to, saves there. |
| SHR-R15.2 | The attached folder never widens anything: it must be inside the agent's access and open to the person. When it is not, the agent says so in words ("Data Checker cannot use this folder") and, for an Editor of the agent, offers "Add it to its access", which is the SHR-R14.7 change. |
| SHR-R15.3 | There is no persistent current folder: nothing carries over between requests, and nothing is per agent (decision 9). |

### SHR-R16 Connectors an agent may use

| ID | Requirement |
|---|---|
| SHR-R16.1 | A connector is set up once, the way ADR-238 D5 sets up every connector: a **site connector** by a site-admin (a shared mailbox, a storage service, a tracker), or a **personal connector** by a person for themselves (their own mail or calendar). Connectors are never configured inside an agent and no credential is copied into one (decision 4). |
| SHR-R16.2 | A site connector is shared with the same roles: Viewer (sees it exists and its status), **Invoker** (may use it, directly or through an agent working for them), Editor (reconnects and changes it), Owner (who set it up). Its general access line is Restricted or Everyone at the site · Invoker. This closes prd-connections FR-007 ("Phase 1: all connections available to all users"). |
| SHR-R16.3 | An agent's **Connectors** list names which connectors it may use and, for each, which of the connector's capabilities (for example "read" but not "send"). A personal connector appears as a slot, "the requester's own <kind>", never as one person's connector. |
| SHR-R16.4 | **Whose credential a run uses:** a site connector's own credential, when the agent's list includes it and the person the run works for is an Invoker of that connector; a personal connector, only that person's own, only when the run works for them, never for an automation another person added, never for another person's request. |
| SHR-R16.5 | **The lesser of two, again:** the capabilities a run may use on a connector are those in the agent's list, those the person may use on it, and those the connector's profile allows (ADR-110 `capability_scope`), intersected. |
| SHR-R16.6 | The Connectors list reads as one line: "May read the site's shared mail; may read your own calendar when it works for you." |
| SHR-R16.7 | A connector that is missing, not verified or needs reconnecting does not break the agent: the step that needs it is skipped loudly (ADR-171) and the person who answers for the connector sees the fix. |

### SHR-R17 The sandbox: how the access becomes enforcement

Two scopes work together. The **access** is what a person sets and reads (SHR-R14, SHR-R16). The
**sandbox** is how the node enforces it on each agent run; a person never sees or sets it, as
`agents/pipeline/sandbox.py` already intends ("reach is the contract; sandbox is the
implementation").

| ID | Requirement |
|---|---|
| SHR-R17.1 | For every agent run the node compiles one **run reach** from: the agent's access, the person it works for, the agent's capabilities and the step's needs. The compilation is pure (no I/O), recorded with the run, and the same for every surface. |
| SHR-R17.2 | **The Library is reached through skills only.** Every read and write of an item goes through the Library's skills and the one decision point (§10, step 1), checked per item at call time. The Library's storage root is never mounted, opened or named inside a run. |
| SHR-R17.3 | **When a tool needs real files** (a converter, a script, a coding harness in an ADR-250 workspace), the node materialises only what the step asked for and passed the check, through the Library's content door (checksum, label and clearance enforced): `in/` read-only, `out/` writable (each file in it is uploaded on finish, through the full upload pipeline, into the agent's own folder as the person the run worked for), `scratch/` writable and deleted when the run ends. Nothing else from the host is visible. |
| SHR-R17.4 | **Network:** none by default. A connector call goes through the node's connector fabric (the run asks the node; the node holds the credential and calls out), so no credential and no general network enter the sandbox, as ADR-250 D4 already does for forge credentials. |
| SHR-R17.5 | **The class** follows `classify_reach`: skills only, no files materialised: `none` (enforced by the decision point); files read: `read_only`; files written or a connector socket: `ephemeral_container`; a system write is refused for agents. |
| SHR-R17.6 | **When this host cannot enforce the class a step needs** (macOS today: `axi agents status` reports NOT APPLIED; ADR-036 D10), that step is refused with the reason and the rest of the run continues; it never runs unconfined (decision 6). The person who answers for the agent and the site-admins see this once, with the fix (DC-17). |
| SHR-R17.7 | Every escape attempt (a path or host outside the reach) is refused and recorded as a `ReachViolation` on the run. |
| SHR-R17.8 | "Sandbox" here means a run's confinement. ADR-195's "sandbox" (the database `kit-up` creates) is a different thing; documents and screens say "kit sandbox" for that one. |

| Piece | Exists today | Missing |
|---|---|---|
| Reach vocabulary | `StepReach` (reads, writes, network globs), `classify_reach`, `summarize_reach`, `SandboxSpec` in `agents/pipeline/` | A per-run reach compiled from an agent's access (today reach is declared per step by hand) |
| Enforcement | `NoSandboxEnforcer` detects violations only; Phase 2 containers and VMs not built | A real enforcer for `read_only` and `ephemeral_container` |
| Per-agent confinement | `agents/sandbox_spawn.py`: a transient systemd unit per dispatch on Linux, `[agent.sandbox]` relaxations, `infra/services.py` `sandbox_directives` (ProtectSystem strict, ProtectHome read-only, network on or off) | A per-run (not per-manifest) path list; a host allow-list instead of network on or off; macOS and Windows confinement |
| Workspace | ADR-250 D1 and D3 (proposed): `docker exec` into a claimed workspace, a grant that expires with the run | Materialising `in/`, `out/`, `scratch/` through the content door |
| Connector egress | Connectors call out from the node (ADR-057, ADR-110 `connection_ref`) | A run-scoped door for connector calls, checked against the run's reach |
| The access itself | Nothing: an agent has capabilities, not files or connectors | The Access record, its skills, the sentence |

## 7. Administration (ADR-142 rule 8, read for documents and agents)

| A site-admin may | A site-admin may not |
|---|---|
| Set the site's sharing policy: the default general access, whether Everyone at the site may be Editor, whether editors may share by default | Open, preview, download or search a Restricted item they have no role on |
| Hold Book manager: rename, move or hide a scheme category | Change, move, delete or re-label another person's item |
| Pause or retire any agent (stopping is safe); see every agent's Team row and who answers for it | Change an agent's instructions, tools or Ceiling, or give it work, beyond their own role |
| Through the offboarding step only, transfer a departing person's items, folders and agents to a named person | Give themselves a role on an item or agent, or take ownership for themselves |
| See that a share happened (the record), without the content | Release an export-control hold (only the reviewer, unchanged) |

Access admin (`admin`, ADR-261) governs who holds roles at the site and reads nothing; it may run the
offboarding transfer and nothing else here. Every administrative act is recorded and the Owner told.

## 8. Migration (expand-only; nobody loses access without being told)

| Today | Becomes |
|---|---|
| `visibility = site` | Everyone at the site · Viewer; `visibility` kept and written in step until every reader has moved |
| `visibility = private` | Restricted |
| `owner` | Owner |
| A person's folder (`library_folders.kind = user`) | Owned by `created_by`, Restricted. Every file in it keeps its own access, so no file is lost to anyone; only the folder's name leaves other people's tree, and they are told once |
| A scheme category override | Unchanged; future changes need Book manager |
| An agent's accountable person | Owner |
| An agent with no accountable person | Keeps running as today and stays in Team's "Nobody answers for" finding until someone is assigned; it does not stop |
| Anyone at the site could talk to any agent | Each agent gets Everyone at the site · Invoker |
| A schedule with no person | Works for the agent's Owner; if there is none, it keeps today's behaviour and raises one ask for a site-admin to name one |
| What #1952 allowed | Stops: moving another person's file, deleting or renaming another person's folder, renaming a scheme category without Book manager. The release note says so |

## 9. Decisions (decided by the owner 2026-11-13 as recommended; most important first)

**Decided by the owner, 2026-11-13:** the Library item view is one component in two sizes, the
Dropbox way (SHR-R9.6, approved as drawn); the Timeline docks at the foot of the screen on pages
other than Log (appkit #217); cloning an agent offers **Also copy its access**, on by default
(SHR-R14a.1). Still open, below.

1. **Whose authority an agent run uses.** (a) The agent Owner's ("acts as me", Apps Script's
   "Execute as me"): simple, but anyone with Invoker borrows the Owner's whole access. (b) The
   starter's: the Owner's limits on the agent stop holding. (c) **The lesser of the two**, an
   automation working for the person who added it and re-checked at every fire. **Recommended: (c).**
   It is the only one where giving someone a role on an agent cannot give them more than they had,
   and it matches ADR-250's attenuating baton. A deliberate "acts as its Owner" mode, signed and shown
   on the agent, could come later for a service agent; not now.
2. **An agent's default file access.** (a) **Everything shared with the site (and anything shared
   wider), plus its own folder; not a person's own Restricted files unless an entry names them.**
   (b) Nothing until someone adds folders. (c) Everything the person it works for can open,
   including their private files. **Recommended: (a)**, the owner's steer: as much latitude as is
   still safe. (b) is the "loser" default, an agent that can do nothing on day one. (c) puts every
   person's private files within reach of any agent they talk to, which is the one place a mistake
   or a prompt injection costs most. With the lesser-of rule, (a) can never show anyone something
   they could not already open.
3. **Is the agent privilege a fifth role, or a checkbox?** (a) **A role in the ladder** between
   Commenter and Editor; Editor includes it; one question in the dialog. (b) A checkbox on any role
   (truer to Google Cloud, where `roles/run.invoker` is separate from edit). (c) Implied by Editor, with
   no lower way to get it. **Recommended: (a)**, because most people need to give a shared agent work
   without changing it, and an Editor can schedule work by editing anyway. **Its name:** "Invoker"
   (Google Cloud's own word for exactly this, and Axiom's `invoke` scope), "Executor" (the owner's
   word, but it collides with the schedule executor and ADR-250's `executor` field), or "Run" (banned
   bare by DC-20). **Recommended: Invoker**, with its line in the dialog in plain words, "Can give it
   work".
4. **Connectors: set up once and granted, or per agent?** (a) **Set up once, as ADR-238 D5 already
   says: site connectors by a site-admin, personal connectors by a person for themselves; an agent
   is granted use of specific ones, with specific capabilities.** (b) Each agent carries its own
   connector configuration and credential. **Recommended: (a).** One credential, one reconnect, one
   audit row (ADR-057), and no credential copied into an agent. **Whose credential:** a site
   connector's own, when the person the run works for may use it (Invoker on the connector); a
   personal connector only for its own person's runs, never for an automation someone else added.
   **Who may use a site connector** follows the same roles (Invoker = may use), which closes
   prd-connections FR-007.
5. **Is talking to an agent, or @-mentioning it, giving it work?** (a) **Yes, Invoker.** (b) A Viewer
   may ask questions and only acts need Invoker. (c) Commenter may talk. **Recommended: (a).** A reply
   spends money and reads data, and with decision 1 it reads only what the asker may read anyway; one
   rule is easier to hold than a split by what the agent happens to do.
6. **When a host cannot enforce the sandbox a step needs** (macOS today). (a) **Refuse that step,
   with the reason; the rest of the run continues; the person who answers for the agent sees it
   once.** (b) Run it unconfined with a warning. (c) Refuse every agent run on that host.
   **Recommended: (a).** Skills reached through the decision point are still safe there; only a
   step that needs real files or a network socket needs the OS, and running it unconfined is
   exactly the case the sandbox exists for.
7. **Defaults for general access.** Documents: (a) **keep today's, Everyone at the site · Viewer, as
   a site setting**, (b) Restricted, as Google. Agents a person makes: **Restricted**; agents an
   extension declares: Everyone at the site · Invoker. **Recommended: (a) for documents** (the Library
   is the site's shared home and ADR-209 D2 chose it; Google's default would quietly hide every new
   upload), **Restricted for new person-made agents.**
8. **The agent's own folder and what it makes.** (a) **Home › Agents › <name>, owned by the
   person who answers for the agent; each file in it owned by the person the run worked for, with
   the site's default general access.** (b) Files owned by the agent (an agent is never an Owner
   today). (c) No agent folder; files go where the person is. **Recommended: (a)**: a file's owner
   stays a person, the folder gives "Made by an agent" (appkit #201) its home, and it is the one
   place an agent may always write.
9. **A current folder.** (a) **The folder you are in when you ask is attached to that request as
   a removable chip; nothing persists.** (b) A per-agent or per-conversation current folder, like a
   shell's working directory. (c) None. **Recommended: (a).** It gives "do this here" without
   hidden state that one person sets and another inherits on a shared agent, and it never widens
   access (SHR-R15). Against (b): Axiom has no working directory today, and a shared agent with a
   remembered location would act in the last person's folder.
10. **Filing (LIB-R4.4, #1952).** (a) **Moving another person's file needs Editor on it**; renaming a
   scheme category needs Book manager. (b) Filing is a site act anyone may do. **Recommended: (a)**,
   because a move is visible to everyone and the Book is shared; a Viewer who thinks a file is
   misfiled asks, or, once comments exist, suggests.
11. **Org-wide and wider.** (a) **Site only**: Restricted or Everyone at the site. (b) Add an
   organization line (every site of the owning org). (c) Add "Anyone with the link". **Recommended:
   (a).** A site is a silo (ADR-205 D4) and public is discovery only (ADR-209 D9); sharing across
   sites is a program (ADR-205), decided in its own ADR.
12. **Can an agent be shared with another agent?** (a) No, people and groups only. (b) **Invoker
   only, never onward**, with the original person's authority carried through. (c) Any role.
   **Recommended: (b)**, so one agent can ask another for work, as the approved chat design shows,
   without any agent ever changing another.
13. **Administrator powers.** As §7: policy, Book manager, pause and retire, and the offboarding
   transfer; never content. **Recommended: as §7.** The open part: whether the offboarding transfer
   needs the departing person's manager to confirm (recommended: the site-admin names the person, the
   new Owner is told, no second approval).
14. **"Edit", not "Tune".** The owner said "editing". The approved people-and-agents design calls
   the agent page's view "Tune". (a) **Rename the view "Edit"** (it holds what the agent does, what
   it may do, its Access and its dials). (b) Keep "Tune". **Recommended: (a)**, the owner's word; it
   is a label change on an approved, unbuilt design.
15. **The Library as a tree from Home.** (a) **A view over the Library's records** (Home: My files,
   Shared with me, Data Book, Agents), no real file system underneath. (b) A real directory tree on
   disk. **Recommended: (a).** It is what people expect to see, it costs nothing in the store, and
   a real tree would be one more path around the decision point (SHR-R17.2).
16. **Commenter before comments exist.** Neither the Library nor an agent has comments today. (a) Offer
   Commenter now (it reads as Viewer until comments ship). (b) **Offer it when comments ship.**
   **Recommended: (b)** (DC-17: no choice that changes nothing). The model and storage carry it now.
17. **What cloning copies of an agent's access** (the default, on, is decided). (a) **Copy what the
    person cloning could have added by hand: entries they can open, site connectors they may use,
    and the "requester's own" slots; never a person's own connector; list everything not copied by
    name, with why and how to ask.** (b) Copy everything and let each run refuse what its person
    cannot reach. (c) Copy nothing. **Recommended: (a).** (b) leaves a clone holding entries nobody
    who made it chose or could see; (c) throws away the fine-tuning that is the reason to clone.
    Listed last only because its default is already decided.

## 10. Build order (after the owner decides; each unit test-first)

1. The decision point and the three tables (`axiom.governance.access`, authz migration, expand-only),
   with the verb table, and every Library verb calling it; #1952's five verbs are covered by the
   table, with a test per verb (LIB-A8, LIB-A9).
2. Migration of Library ownership and visibility; `visibility` written in step.
3. `library.share`, `library.access`, `library.transfer` skills (CLI, MCP, chat); requests and their
   asks; KEEP's expiry.
4. Retrieval filter for direct shares (SHR-R8.3).
5. The web: the share dialog, People with access, the access column words, the Owner filter, Shared
   with me, the drawer's Access section, the no-access and request screens (appkit).
6. Agents: the agent verb table on the agent skills; `ScheduleDefinition` records the person; the
   executor builds the delegated principal; `agents.share`, `agents.access`; Team's filter and role.
7. FGA types `document`, `folder`, `agent` and the tuple mirror (ADR-209 D7), when the substrate is on.
8. An agent's Access: the record (files and connectors), `agents.access_set` and `agents.access_show`
   (CLI, MCP, chat), the sentence, the decision point's "in the agent's access" check on every
   Library skill a run calls. This alone enforces files for skills-only agents on every host.
9. Connector sharing (site connectors with roles, personal connectors as slots) and the run-scoped
   connector door on the node.
10. The run reach compiler (access, person, capabilities, step needs to `StepReach`), recorded with
    the run; `ReachViolation` on every refusal.
11. Materialising `in/`, `out/`, `scratch/` through the content door into an ADR-250 workspace, and a
    real enforcer for `read_only` and `ephemeral_container`; the per-run path list for Linux's
    transient unit; a host allow-list instead of network on or off.
12. macOS and Windows confinement; until then, SHR-R17.6.

## 11. Acceptance

| ID | Acceptance |
|---|---|
| SHR-A1 | A colleague with no role gets not-found for a Restricted item on every surface (page, CLI, MCP, chat); with Viewer they read it and every change is refused naming the role needed. |
| SHR-A2 | A Viewer's `library.move`, folder rename and folder delete are refused; an Editor's move succeeds and is recorded; a scheme category rename by anyone but a Book manager is refused (#1952). |
| SHR-A3 | Deleting a folder that holds a subfolder and files moves all of them to Deleted files in one undoable act; a held item stays, and the answer says so; restoring the folder restores everything. |
| SHR-A4 | A share on a folder reaches every item in it; an item cannot be given less than its folder. A scheme category confers nothing. |
| SHR-A5 | With "Editors can change permissions and share" off, an Editor's share is refused; with download and copy off, a Viewer's download and copy are refused. |
| SHR-A6 | An expired entry ends on its day and its end is in the activity; KEEP's reminder went to the person who gave it. |
| SHR-A7 | A request reaches the Owner's Needs you; Share with a lower role grants that role; Decline shows the requester who, when and the reason, and Request again works at once. |
| SHR-A8 | A held item's shares are not used while held and return on release; a share to a person not cleared for the label is refused naming the label. |
| SHR-A9 | Search and chat retrieval return a directly shared item to the person it was shared with and to nobody else. |
| SHR-A10 | A site-admin cannot open a Restricted item, cannot give themselves a role, can transfer a departing person's items through offboarding, and each act is recorded and the Owner told. |
| SHR-A11 | Migration: every person can read exactly the files they could before; only the folder names in §8 leave others' trees. |
| SHR-A12 | An agent's Team row shows the same fields to everyone at the site; its Tune is absent from the response for someone with no role. |
| SHR-A13 | An Invoker can give an agent work and add an automation; cannot change its instructions; an Editor can; only the Owner raises its Ceiling. |
| SHR-A14 | An agent run started by a person reads nothing that person cannot read, even when the agent's Owner can; an automation stops and raises one ask when the person who added it loses Invoker. |
| SHR-A15 | No scheduled fire runs without a person it works for (negative control: a schedule with none is refused, or, during migration, raises the ask). |
| SHR-A16 | Every share change is a `decide()` receipt, listed by `axi grant list` as kind `share`, and revocable with `axi grant revoke`. |
| SHR-A17 | A new agent's access is the site-wide entry plus its own folder; a run reads a site file, cannot read the requester's own Restricted file, and writes only in its own folder. |
| SHR-A18 | Adding a folder the person cannot open to an agent gives that person nothing: their run is refused it. Narrowing to "only Data" refuses a site file outside Data. |
| SHR-A19 | A viewer of an agent sees only the access entries they can open, and the rest counted. |
| SHR-A20 | A request made from a folder attaches it; a folder outside the agent's access is refused with the offer to add it, for an Editor only; nothing carries to the next request. |
| SHR-A21 | A run uses a site connector only when the agent lists it and the person may use it; a personal connector only for its own person's runs; never for an automation another person added. |
| SHR-A22 | A run never sees the Library's storage root, a credential or the network; a step needing files sees only `in/`, `out/`, `scratch/`; an attempt outside is refused and recorded. |
| SHR-A23 | On a host that cannot confine, a step that needs files or a network socket is refused with the reason, and the rest of the run completes. |
| SHR-A24 | Every parity inventory row the original or the build has is present in the page after this change (a checklist test over the inventory ids), and the item surface steps with ←/→, expands and returns. |
| SHR-A25 | Cloning with "Also copy its access" copies exactly the entries and site connectors the person could add, never a person's own connector, and lists each one not copied by name with how to ask; unticked, the clone has the default access. |
