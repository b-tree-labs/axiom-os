# Units, and the names that carry them

A feed on the node has six channels: `corrected_cm`, `corrected_cm_interp`,
`measured_cm`, `predicted_cm`, `predicted_cm_interp`, `rom_matched_cm`. They
are all centimetres. **Two of the six declare it.** The surface showing them
said "no unit" six times — twice redundantly, four times uselessly.

That one screen holds every question worth answering about units, so this is
the answer to all of them.

## 1. A unit is a field. A name is a label.

**The unit lives in the `unit` field and nowhere else is authoritative.**

A channel name is what somebody else's instrument calls a thing. We do not
rewrite it: a rename forks the data, and this programme has already paid for
that once. So the name is never normalised, never stripped of a unit token, and
never treated as the source of truth.

**And the unit is never inferred into the data.** Reading `cm` off a name and
writing it into the `unit` column would turn a guess into a measurement, which
is the exact failure the unit rule exists to stop. A value that reads as a fact
must have come from a declaration.

## 2. But a name that carries a unit is evidence

Ignoring it is the other mistake. `measured_cm` with an empty unit field is not
"a channel with no unit" — it is a channel whose own name says centimetres
while its declaration is silent. That is a finding with a fix attached.

Three outcomes, and the platform distinguishes them
(`scidisplay/unit_names.py`):

| the name | the declaration | what it is | what to do |
|---|---|---|---|
| says `cm` | says `cm` | **redundant** | show it once |
| says `cm` | silent | **an omission, with its fix named** | add the line to the channel map |
| says `mm` | says `m` | **a conflict** | stop; a figure wrong by a thousand looks fine |

The third is the serious one and has no equivalent in "this has no unit". A
declaration that contradicts the name is worse than one that is missing,
because nothing about the result looks wrong.

Ambiguous tokens are deliberately absent from the table. `valve_position` is
not picometres. A table that guesses is worse than one that stays quiet: a
wrong unit is a wrong number, and an absent one is at least visibly absent.

## 3. Naming, for data we create

For anything the platform or a site names itself, rather than inherits:

- **Do not put the unit in the name.** `corrected` with `unit = "cm"`, not
  `corrected_cm`. The unit belongs in the field that can be corrected without
  a rename, that a machine can convert, and that can express `L/min` or
  `n/cm²/s` — none of which a name can.
- **Never say it twice.** `corrected_cm` *and* `unit = "cm"` is two places to
  change and one of them will be missed.
- **Never encode it only in a table name.** A table is a set of channels, and
  channels differ.
- A partner's names are theirs. We keep them, and the checks above are how we
  make them useful.

## 4. Where absence is shown, and how

An absent unit is always stated — a bare number reads as a fact, and 23.2 of
70.1 million served rows carried none. The question this screen raised is how
often to say it.

**Once per group where a person chooses, always where a number is read.**

- **A picker** says it once for the set: *"4 of 6 here declare no unit — 4 name
  one their declaration omits (cm)."* One line is information; a word on every
  chip is decoration, and decoration is what a person learns to stop reading.
  That is the worst thing a warning can become.
- **An axis** always says it, every time. It is where a number becomes a
  reading, so it never gets the short form.
- **A chip, a cell, a label** says a unit only when it adds one. A name that
  already carries it does not get it printed alongside.
- **Amber, not red.** It marks an absence, not an error. The data is real; the
  declaration is missing.

## 5. Where this gets fixed

Not in the renderer, and not in the browser. A missing unit is fixed **in the
channel map**, which is the site's own declaration of what its channels are and
what they are measured in. Everything downstream reads it.

So the useful output of the checks above is not a warning on a figure. It is a
list of channel-map lines somebody has to write, and the name is what says
which ones.

---

Checked by `scidisplay/tests/test_a_name_that_carries_a_unit.py`, including the
six channels that prompted this, as the node actually holds them.
