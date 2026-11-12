# Names for sites, nodes, connectors and feeds

Adopted 2026-11-09. One pattern for everything a site deployment names, so a
name tells a reader what a thing is and whose it is. The examples use the
placeholders from [naming and the public surface](naming-and-the-public-surface.md).
Real names belong in the site record (ADR-178) and the site's own repository.

| Thing | Pattern | Example | Can it change? |
|---|---|---|---|
| Site | `<org>-<asset>` | `senna-loop` | No |
| Staging twin of a site | `<site>-staging` | `senna-loop-staging` | No |
| Node | `@<role>-<n>:<site>` | `@daq-1:senna-loop` | Retire and replace |
| Single-purpose service principal | `@<role>:<site>` | `@forwarder:senna-loop` | Retire and replace |
| Connector | `<site>-<source>` | `senna-loop-daq`, `senna-loop-box` | No |
| Feed | `<org>.<system>` | `senna.epics` | No |
| Ingest endpoint | `ingest.<domain>` | `ingest.example.org` | Only by moving the name |

## Rules

- Names are lowercase ASCII, with hyphens between words.
- `<org>` is a short name for the owning organization, and `<asset>` is the thing
  measured. A site has one id everywhere: no alias in another repository, no lab
  name in place of the organization.
- `<role>` says what the node does. Use `daq` for a site's acquisition node.
  `<n>` starts at 1 and is never reused.
- `<source>` is how the data arrives or which system produces it, such as `daq`,
  `box` or `console`.
- There is one credential per node, bound to its site, and it carries the node's
  principal. ADR-177's rule stands: the site comes from the credential.
- Every site uses the same ingest endpoint. The site is never in the URL.
- A machine may have any hostname or nickname. Nothing may depend on it.

## Names that cannot change

Site ids and feeds are part of how a stored reading is identified, and a
connector name keys its bronze folder and read position. Renaming any of them
strands data or loads it twice.

Existing names that do not follow this pattern stay as they are. A new connector
gets a correct name when it is created, and an old one leaves when it is retired.
