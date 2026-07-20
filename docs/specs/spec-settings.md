# Axiom Settings Surface Spec

> 🔲 **SPEC'D** — 2026-05-22. Unifies the three existing user-facing
> config commands (`axi config`, `axi settings`, `axi connect`) into
> a single `axi settings` surface with extension-registered sections.
> Direction endorsed by Ben per 2026-05-22 conversation; canonical
> rule lives at [[feedback_axi_settings_unification]].

---

## 1. Problem Statement

Today there are three commands that all touch user configuration:

| Command | Today's role | File |
|---|---|---|
| `axi config` | Interactive onboarding wizard | `axiom/setup/cli.py` + `axiom/setup/wizard.py` |
| `axi settings` | Key-value store with dotted-key sections (`routing.default_mode`) | `axiom/extensions/builtins/settings/` |
| `axi connect` | Connection (LLM + RAG + auth) preset wiring | `axiom/cli/connect.py` |

Three problems:

1. **User cognitive load.** A new user (Austin, 2026-05-22 onboarding pass) has to know which of three commands owns "set my API key" / "pick my default LLM" / "configure the RAG endpoint." None of the three names obviously owns any of those concerns.
2. **Extension-side knobs have no canonical home.** When an extension wants to expose a configurable setting (e.g., classroom's instructor-mode toggle, signal's polling interval), today it scatters as either a CLI subcommand, a raw env var, or an undocumented `runtime/config/<thing>.toml`. There's no consistent place for "show me my classroom settings."
3. **The wizard, the store, and the connection presets are conceptually all "settings."** The split is implementation-shaped, not user-shaped.

---

## 2. Design

### 2.1 One surface

`axi settings` becomes the single user-facing surface for all
configuration. The existing `axi config` and `axi connect` commands
remain as aliases for back-compat (see §5 Migration), with a
deprecation banner pointing at the new surface.

```
axi settings                              List installed-and-configured sections
axi settings <section>                    Show one section's current values
axi settings <section> set <key> <value>  Edit one key
axi settings <section> reset <key>        Remove the value for one key
axi settings setup                        Run the interactive onboarding wizard
axi settings setup <section>              Run the wizard scoped to one section
axi config                                Alias for `axi settings setup` (deprecation banner)
```

### 2.2 Extension-registered sections

Sections are registered the same way connections + commands are
registered today: via an AEOS manifest block.

```toml
# axiom-extension.toml in some extension
[[settings.sections]]
name = "routing"
display_name = "LLM routing"
description = "Provider preferences, sensitivity mode, tier defaults"
entry = "axiom.extensions.builtins.chat.settings_section:get_section"
intent_groups = ["start"]  # progressive disclosure per spec-aeos
```

The `entry` callable returns a `SectionView`:

```python
@dataclass(frozen=True)
class SectionView:
    name: str                       # canonical id (matches the manifest name)
    display_name: str               # user-facing label
    description: str                # one-line section description
    values: dict[str, Any]          # current settings under this section
    summary: str                    # one-line "what's configured here" for top-level listing
    is_active: bool                 # see §2.3
    wizard: WizardCallable | None   # optional setup-wizard callable
```

### 2.3 Visibility rules — "installed AND has values"

A section appears in `axi settings` listing output ONLY when:

1. The owning extension is installed (manifest discovered via the standard 3-tier extension discovery), AND
2. `SectionView.is_active` returns True (typical implementation: returns `True` iff `values` is non-empty OR the section has a non-default summary).

This keeps the top-level listing focused on what the user has *actually configured*, not every theoretical knob across every installed extension. Users who want to enumerate possibilities use `axi settings --all` (omits the `is_active` filter).

### 2.4 The core/general section

Settings that don't belong to any extension (today's flat
`routing.default_mode`-style keys) live in a built-in `general`
section served by the `settings` builtin extension itself. Same
contract; no special-casing.

---

## 3. Commands in detail

### 3.1 `axi settings` (list)

```
$ axi settings

📋 Active settings
   general              routing.default_mode=auto · sensitivity=balanced
   routing              3 keys set
   connections          2 of 6 presets configured (anthropic, internal-llm)
   classroom            instructor-mode=on · 1 active class

   `axi settings <section>` to drill in.
   `axi settings --all`     to include unconfigured sections.
```

One row per active section. The right-hand column is the section's
`summary`. Output sorted: `general` first, then alphabetical.

### 3.2 `axi settings <section>` (view)

```
$ axi settings routing

[routing]
   default_mode             auto
   sensitivity              balanced
   tier_hint_default        standard

   `axi settings routing set <key> <value>` to edit.
   `axi settings routing reset <key>` to remove an override.
```

### 3.3 `axi settings <section> set <key> <value>`

Validates the key against the section's schema (if declared) and
writes via the section's writer callable. Boolean values accept
`true`/`false`/`yes`/`no`/`on`/`off`; numeric values are parsed
type-checked.

### 3.4 `axi settings setup` (the wizard)

Runs the interactive onboarding wizard that `axi config` runs
today, with one change: each section's `wizard` callable (if
declared) is invoked in turn instead of one monolithic flow.
Extensions therefore own their own wizard slice, registered the
same way as the section view.

```
$ axi settings setup

   🧭 Axiom setup (wizard)

   ── general ────────────────────────────────────────────
   …existing onboarding prompts…

   ── routing ────────────────────────────────────────────
   …routing-extension prompts…

   …
```

### 3.5 `axi settings setup <section>`

Runs only the named section's wizard. Useful for re-doing a single
section after declining it earlier or for first-time setup of an
extension installed post-initial-onboarding.

### 3.6 `axi config` (deprecated alias)

```
$ axi config
   Note: `axi config` is now `axi settings setup`. Aliased for
   back-compat; the new name is the canonical surface.
   Running `axi settings setup`…
   …
```

Banner shown once per session, then suppressed via a marker file.

---

## 4. Section-registration API

### 4.1 Manifest block

```toml
[[settings.sections]]
name = "routing"
display_name = "LLM routing"
description = "Provider preferences, sensitivity mode, tier defaults"
entry = "<module:function>"             # returns SectionView
wizard = "<module:function>"            # optional; returns SectionView after running
schema = "<module:SCHEMA_DICT>"         # optional; pydantic-like schema for validation
intent_groups = ["start"]               # progressive disclosure
```

`schema` is optional but recommended: it gates `set` against typos
and out-of-range values. Without it, `set` accepts any string and
defers validation to the writer callable.

### 4.2 Discovery

Sections are discovered through the existing 3-tier extension
discovery (`axiom.extensions.discovery`) — same as commands +
connections today. A new `discover_settings_sections()` helper
mirrors `discover_connections()`.

### 4.3 Built-in registration

The `settings` builtin extension itself registers the `general`
section. Future built-in extensions register additional sections
the same way as any other extension.

---

## 5. Migration

### 5.1 Back-compat aliases

- `axi config` → `axi settings setup` (banner; both shipped)
- `axi connect` → keeps working unchanged in v1.x; folded into
  `axi settings connections set <preset>` semantics in v2.x once
  consumer migration is complete. Specified separately if needed.
- Existing `runtime/config/settings.toml` continues to be the
  on-disk store for SettingsStore-managed keys; the
  section-registration API reads from there for the `general`
  section.

### 5.2 Telemetry-free migration

No usage-counting required. The deprecation banner runs once per
TTY session (marker at `~/.axi/state/config-alias-shown`). After
two release cycles with the banner, `axi config` becomes a hard
error pointing at `axi settings setup`.

---

## 6. Test plan for the implementation PR (informative)

The implementation PR is expected to derive its test cases from
this spec, written test-first. Minimum coverage:

| Behavior | Test |
|---|---|
| `axi settings` lists only active sections | mock 3 sections (1 inactive) → output has 2 rows |
| `axi settings <section>` shows that section's values | mock 1 section with 2 keys → values printed in order |
| `axi settings <section> set k v` writes via section writer + persists | mock section writer; call; assert writer invoked + persisted |
| `axi settings setup` invokes per-section wizards in order | mock 2 sections w/ wizards; assert call order |
| `axi config` aliases to `axi settings setup` + emits banner | call `axi config` → assert setup runs + banner shown |
| Section with `is_active=False` omitted from `axi settings` listing | mock 1 active + 1 inactive → only active shows |
| Sections discovered via 3-tier discovery | drop a section manifest in `.neut/extensions/`; assert it surfaces |
| Schema validation rejects bad `set` | section with `tier_hint: simple|standard|...`; `set tier_hint foo` → error |

---

## 7. Open Items

- **`axi connect` final shape.** The cleanest fold puts each
  installed connection preset into the `connections` settings
  section (e.g., `axi settings connections set anthropic-key
  <key>`). Spec'd here as v2.x; v1.x leaves `axi connect` alone.
- **Per-extension settings schema validation.** `schema` is an
  optional manifest field; the format (pydantic-like? raw JSON
  Schema? a custom DSL?) is deferred to the implementation PR's
  design call.
- **`/settings` slash command in chat.** A natural follow-on
  surface: same listing + edit semantics inside a chat session.
  Lives in `spec-chat-model-picker.md` follow-up or its own spec.
- **Settings export/import.** `axi settings export > my-settings.toml`
  for backup + machine-to-machine config copy. Out of scope here.

---

## 8. Related Documents

- [[spec-connections]] — connection preset registration + credentials
  (overlapping surface; v2.x folds connect into settings)
- [[spec-aeos-0.1]] — `[[settings.sections]]` is a new manifest
  block; AEOS schema needs the field added when this lands
- [[feedback_axi_settings_unification]] — canonical rule + the
  endorsement context
- [[feedback_proactive_ux_minimize_cognitive_load]] — design
  motivator: don't make users learn 3 commands when 1 will do

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
