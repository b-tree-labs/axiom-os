# ADR-261: The role that governs a site is `site-admin`, and `admin` reads as "Access admin"

**Status:** Accepted (owner decision, 2026-11-12)
**Supersedes:** the role naming of [ADR-151](adr-151-roles-separate-governing-from-reading.md) (`operator`) and of [ADR-222](adr-222-three-persona-roles-and-a-release-check-that-uses-them.md) (the third persona role). Their rights, and every other decision in them, stand.
**Related:** [ADR-153](adr-153-a-role-is-granted-at-a-site.md) (a role is granted at a site), [ADR-103](adr-103-directory-providers-group-and-membership-resolution.md) (group-to-role maps), [ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md) (logbook signing roles are a logbook's own)

## Context

ADR-151 named six roles and ADR-222 three persona roles. One of them, `operator`, reads,
invokes and governs at a site (`*:read`, `*:invoke`, `*:govern`). Another, `admin`, governs
access and reads nothing (`*:govern`).

The word "operator" names a job in consumer domains: a person licensed or trained to run
something physical. A consumer that puts a platform screen in front of those people shows
"Operator" beside a person who holds the platform role, and the two readings collide. The
person reading cannot tell whether the label is a permission or a job title, and in a
consumer where the job carries legal weight the wrong reading is costly in both directions.

"Admin" has the opposite problem: it is the common word, and it is the narrow role. A person
granted `admin` expects to see data and does not.

## Decision

1. **The role is `site-admin`.** Its scopes are unchanged: `*:read`, `*:invoke`, `*:govern`,
   granted at a site (ADR-153). It is displayed as **"Site admin"**. It remains the top of the
   persona ladder: `reader`, `contributor`, `site-admin`.
2. **`admin` keeps its id and is displayed as "Access admin".** It governs access and reads
   nothing, as ADR-151 decided.
3. **`operator` is a retired id, read as an alias.** Every place that reads a role reads it
   through one function (`axiom.governance.roles.canonical_role`): the role registry and its
   override file, the gate's key minting and invitations, the authz rules and the actor
   context, the switchboard's role sets and its `role` cohort attribute, approvals, settings
   and team visibility, directory group-to-role maps, dev personas and help personas. A grant
   stored as `operator` on a deployed node or in a config file therefore keeps working, with
   exactly the rights of `site-admin`.
4. **Stored grants are not migrated in place.** Reading never rewrites. New grants are written
   as `site-admin`. A command that names `operator` (`axi gate role --set operator`, `adduser
   --role operator`, `invite --role operator`, `axi role add operator`) is accepted and prints
   one line naming `site-admin`. A role bundle named `operator` is refused at registration, so
   the retired id cannot come back as a second role with different rights.
5. **Display names live in one table** (`DISPLAY_NAMES` beside the alias table). CLI listings,
   help text and HTTP refusals that name a role use it.
6. **Other uses of the word are not the role and stay.** A maintenance request's `operator`
   field (who signed it), provenance values such as `resolved_by` and `labelled_by`,
   escalation and follow-up audiences, a logbook's own signing roles (ADR-142), and English
   prose about whoever runs a system. The notification addressing handle `@operator`
   (ADR-238 D9) is a separate namespace and is renamed by its own change.

A guard test (`tests/test_no_platform_role_is_called_operator.py`) fails when shipped code
holds the literal `"operator"` outside a listed, reasoned allowlist, or when shipped help or
living docs name an "operator role".

## Options considered

- **Keep `operator`, display it differently.** Rejected: the id is what people type in
  `--role`, put in group-to-role maps and see in JSON, so the collision survives the label.
- **Rename `admin` too (to `access-admin`).** Rejected for now: the id is not ambiguous with a
  consumer job, and renaming it doubles the alias surface. The display name carries the fix.
- **Migrate stored grants at upgrade.** Rejected: an in-place rewrite of accounts files, maps
  and invitations on every node is a write we would cause, with a rollback to plan. The alias
  costs one function call per read. A later, deliberate rewrite can retire the alias.

## Consequences

- A deployed node keeps every `operator` grant working after upgrade, with no step.
- Downstream layers (products, web front ends, site configuration) still say `operator` in
  places; they work through the alias and are renamed in their own repositories.
- The alias is permanent until a follow-up rewrites stored grants (accounts files, group-to-role
  maps, invitations, roles files, cohort rules, approval policies, `competency.json`, `dev.toml`)
  and then removes the entry from `LEGACY_ROLE_ALIASES`.
