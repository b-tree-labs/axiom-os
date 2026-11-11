# actors — Team

The directory of everyone and everything that can act or sign at a site: people,
agents and nodes, in one list (MVP-23, PRD site-view §20).

- **No store of its own.** A read across the gate's accounts, keys and
  invitations, the directory's group tuples, the agent roster and the node.
  The extension declares no database and writes nothing (MVP-23-A4).
- **Every agent names an accountable person**: its roster declaration
  (`accountable = "@name:context"`) or `AXIOM_AGENT_ACCOUNTABLE`
  (`keep=@ana:site-a,*=@ops:site-a`). One without is listed with a finding.
- **Fields follow the viewer's role** (`visibility.py`): members, stewards
  (`AXIOM_TEAM_STEWARD_ROLES`, a site's own role names) and administrators.
- **Tenancy**: a person is listed only when the viewer's scope permits their
  site, through the one decision (`site_scope.resolve`); counts are taken after.
- **Writes are gate verbs**: invite (`gate.invite`), roles (`gate.role`),
  suspend and reinstate (`gate.suspend`, `gate.reinstate`). None makes anybody sign in
  again.

Surfaces: `axi actors list|show`, `/api/v1/actors`, MCP `axiom_actors__list`,
`axiom_actors__show`. Flag: `team`.
