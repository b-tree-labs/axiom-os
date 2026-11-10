# ADR-243: People see a "log", code keeps `logbook`, and `log` is the command noun for logs

**Status:** Proposed (2026-11-09)
**Related:** [ADR-150](adr-150-an-attestation-record-is-kept-in-a-logbook.md),
[ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md),
[ADR-056](adr-056-skill-as-function.md),
[ADR-010](adr-010-cli-architecture.md),
[ADR-218](adr-218-the-vault-is-one-component-owned-by-keep-under-one-noun.md) (the deprecation pattern reused here),
[prd-attestation.md](../prds/prd-attestation.md) §6b "The command line" (the user-facing design),
[prd-logging.md](../prds/prd-logging.md) (the system log, now "Platform events")

## Context

ADR-142 called the signed record of what people did a "book" (a consumer layer's first ADR
called it an "attestation book"). ADR-150 renamed it **logbook**, so it would not collide with
the Library's Book, and the consumer layer followed. That name is now in the manifest (`kind = "logbook"`), the routes
(`/api/v1/attest/logbooks`), the signed record (`"logbook"`), the database and the tests.

The people who use it do not say "logbook". The rail page is **Log**, the designs say "the Ops
Log", and an operator says "put it in the log". Meanwhile the command line has three names for
two things: logs live under `axi attest` and `axi attest logbook`, the noun `axi log` is the
machine's own system log (`src/axiom/extensions/builtins/log/`), and a consumer layer prints
its log's declaration under a noun of its own. The designs now call the system log **Platform events**.

## Decision

1. **Code keeps `logbook`.** The manifest kind, the `[logbook]` table, `/api/v1/attest/...`
   routes, the record field, module, class, skill and MCP tool names (`attest.*`,
   `axiom_attest__*`) and ADR-150 stay as they are. Nothing is renamed in code.
2. **Everything a person reads says "log".** The web app, docs, CLI help text, prompts and
   messages say "log" (a log, the Log page, a log by its own name). "Logbook" appears in user-facing text
   only where it names code (a manifest key, a route, a file).
3. **`log` is the command noun for logs.** `axi log list`, `axi log <log> show|new|sign|...`,
   and the same under any product's brand (`<cli> log ...`). The grammar, the verbs and the
   example sessions are product design and live in
   [prd-attestation.md §6b](../prds/prd-attestation.md#6b-the-command-line-log).
4. **The system log becomes the log `platform`.** Its verbs move to `axi log platform ...`. It
   is not a signed log; `platform` is a reserved handle that no declared log may take.
5. **A product does not override `log`.** A product's own log is a handle under the platform
   noun (a log with the handle `ops` is `<cli> log ops`). A product noun of its own, even one
   spelled like a log's handle, means its domain, never a log.
6. **One name per thing, with a deprecation period.** Each old command forwards to its new form
   for a stated period, prints one line naming that form, and is then removed. No second
   permanent name is kept.

## Technical design

How the noun change is built. Base unit F5 of the logbook build plan builds it.

### Registration

- The `attest` extension's manifest gains the `cmd` provider `noun = "log"`, entry
  `axiom.extensions.builtins.attest.log_cli:main`. The system log extension
  (`builtins/log/`) drops its `cmd` provider; its argparse handlers stay where they are and are
  imported by `log_cli` for `log platform`. One extension owns the noun, so the extension
  registry never sees two providers of `log`.
- A logbook declaration gains an optional `handle` in `[logbook]` (and the same optional key in
  the manifest's `ProvidedLogbook` schema): the short name a person types. It defaults to the
  id. It matches `^[a-z][a-z0-9-]{0,23}$`, is unique on the node, and may not be a reserved
  word. The registry refuses to load a logbook that breaks any of these, naming the clash.
- Reserved words at the position after `log`: the noun-level verbs (`list`, `timeline`,
  `validate`, `anchor`, `role`, `device`, `location`, `waiting`, `help`), `platform`, and, for
  good, the old system log verbs (`tail`, `verify`, `stats`, `backend`, `sinks`, `export`,
  `trace`) so an old script can never reach a log by accident.

### Dispatch

- `log_cli.main(argv)` reads `argv[0]`. A reserved word dispatches to its noun-level verb. Any
  other word is looked up as a handle in the logbook registry; the remaining argv is parsed by
  the per-log parser, which is the same parser for every log. An unknown word exits 2 with the
  list of handles and the nearest match; a logbook id typed where a handle was expected names
  the handle.
- Every verb calls one skill through the registry (ADR-056), as `axi attest` does today. The
  CLI adds argument parsing and printing only.
- `cli/command_space.py` maps a verb path to a skill by joining words (`data show` to
  `data.show`), which cannot work when the second word is a value. `log_cli` therefore exports
  `SKILL_FOR_VERB` (`{"show": "attest.show", "platform tail": "log.tail", ...}`), and
  `command_space` reads an exported map before it joins words. The second position gets the
  value kind `log`, filled from `attest.logbook_list` by a completion value provider, so Tab
  completes handles.
- Verbs that later units add (supplement, acknowledge, co-sign, seal, search, intervals, import,
  timeline) register by adding a parser function and a `SKILL_FOR_VERB` entry in `log_cli`. A
  product cannot add a verb under `log`; it declares what generic verbs read (export templates,
  import sources, interval labels) in its logbook file.

### Skills, MCP and HTTP

- Skill names, MCP tool names and routes do not change (decision 1). A person types
  `axi log demo show`; an agent calls `axiom_attest__show` with `logbook = "demo_log"`; the
  web app calls `/api/v1/attest/demo_log/records`. Tool descriptions and help text say
  "log".
- Skills accept a handle wherever they accept a logbook id, resolved once in the service, so a
  person can say "the demo log" to chat and the same skill answers.
- The system log's verbs become skills `log.tail`, `log.verify`, `log.stats`, `log.export` with
  surfaces `("cli",)` (read-only, `risk = "read"`), so `command_space` can map them. `log.tail`
  also gets `mcp` and `agent_tool` only if [prd-logging.md](../prds/prd-logging.md) is amended to
  allow it.

### How the old commands forward

- `axi attest` stays a registered noun, hidden from help, whose entry is a forwarder in the
  pattern of `secrets/cli.py` (ADR-218): it maps each old verb and flag set to the new argv,
  prints one stderr line `"<cli> attest new" is now "<cli> log demo new"; the old form is
  removed in vX.Y.0`, then calls `log_cli.main`. `--logbook <id>` becomes the handle.
  `axi attest sign DRAFT`, which names no log, reads the draft's logbook to build the new form.
- The old system log verbs under `axi log` (`tail`, `verify`, `stats`, `backend`, `sinks`,
  `export`, `trace`) forward to `axi log platform <verb>` with the same line.
- A consumer layer's own noun for its log becomes a forwarder to `<cli> log <handle> about` in
  the same way, in the consumer's repository.
- `AXI_NO_DEPRECATION_WARNING=1` silences the line. Exit codes and `--json` output of a forwarded
  command are those of the new form, which keeps the old ones (0, 1, 2) for every existing verb.
- The forwarders are removed two minor releases after the release that ships `log`, and not
  before the consumer layers that pin Axiom use the new forms. The removal release is written into
  the forwarders as a constant, as `CLI_ALIAS_REMOVED_IN` is for the vault.
- Docs, remedies, `next:` lines and error messages name the new forms from the release that
  ships them.

## Consequences

- One noun, `log`, reaches every log and Platform events; the rail page, the help text and
  speech ("log, ops, show") use the same word.
- Code and the record format are unchanged, so no signed history, route or MCP client moves.
  The cost is that code and screen use different words; the glossaries in both repositories
  carry "log" (the term people see) and "logbook" (the code name for a log).
- `axi log tail` changes meaning after the deprecation period: it stays reserved, so it fails
  with a pointer instead of reaching a log.
- A product that wants a verb no generic verb covers has to argue for it in Axiom, which keeps
  the grammar one shape for agents and for completion.
- J1's planned `axi journal` noun is not added; the Timeline is `axi log timeline` (the
  journal stays the code name).
