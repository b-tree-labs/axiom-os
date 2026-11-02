# Tech spec — uncertainty

**Status:** Living. A PR that changes how uncertainty composes updates this
file in the same PR.
**Owning ADR:** ADR-136
**Code:** `src/axiom/uncertainty/`
**Tests:** `tests/uncertainty/` — algebra, field cases, pipeline, proof

---

## 1. What this is for

So that a number this platform serves can say how well it is known, and so
that two such numbers can be combined without the combination being a lie.

The second half is the hard one and the reason this is a primitive rather
than a column. Composition is where uncertainty goes wrong, always in the
direction of overconfidence, and always invisibly.

---

## 2. The contract an extension implements

This is the section a contributing extension needs. Everything else here is
background.

### 2.1 Mint a symbol per independent physical source

```
<extension>:<resource>:<aspect>
```

Lowercase extension, then the resource, then what about it is uncertain.

```
signals:tc-14:repeatability
signals:cal-bath-a:offset
data_platform:resample:step_hold
model_corral:surrogate-v3:truncation
```

**One physical source, one symbol, however many values it touches.** An
instrument's calibration offset is ONE source across every reading it makes.
Giving each reading its own calibration symbol is the single most common way
to get this wrong, and it produces an average that claims the calibration
away.

**Sharing a symbol means sharing a source.** Two sensors do not share
repeatability — that is the per-reading noise of each. They may well share a
calibration bath. Getting this backwards makes the wrong thing cancel, and
the tests in `test_field_cases.py` include a case where it did.

### 2.2 Declare a budget for every symbol

```python
Budget(
    symbol="signals:cal-bath-a:offset",
    measurand="temperature indicated by a sensor calibrated in bath A",
    standard=0.5,
    kind=TYPE_B,
    traceable_to="bath A certificate 2026-03, NIST-traceable",
    valid_over="0 to 200 degC, within 12 months of calibration",
)
```

`measurand` is not optional in spirit even though the dataclass gives it no
default. GUM is explicit that an uncertainty without the quantity it is
about is undefined. "the sensor" is not a measurand; "temperature indicated
by a sensor calibrated in bath A" is.

`valid_over` is the field most often left `unstated`, and the budget
renderer names every term that leaves it that way, because a budget whose
terms do not say where they apply has an unknown range of applicability.

`kind` is `TYPE_A` when evaluated statistically from repeated observation,
`TYPE_B` by any other means (a certificate, a datasheet, a bounded
tolerance, judgement). The distinction is GUM's and it is about *how you
know*, not about quality.

### 2.3 Carry values as `Quantity`

```python
Quantity(value=21.4, unit="degC", terms={
    "signals:cal-bath-a:offset": 0.5,
    "signals:tc-14:repeatability": 0.1,
})
```

Then compose with `add`, `mean`, `difference`, `product`, `scaled`. Mixed
units are refused rather than coerced.

### 2.4 Report absence honestly

| situation | use | behaviour |
|---|---|---|
| sources known | `Quantity` | exact composition, correlation computable |
| `u` known, correlation not | `MagnitudeOnly` | combination yields **bounds** |
| nothing reported | `Unquantified` | **counted**, left outside the bound |

Do not pass zero for unknown. Zero is a claim of perfect precision — the
same reasoning that made `silver.signals.uncertainty` nullable rather than
defaulted.

Do not pass a fault code as a value. A fault code is `Unquantified`, not a
reading with unknown error.

### 2.5 If your extension is a pipeline stage

Use `axiom.uncertainty.pipeline`. Declare stages by the kind of ignorance
you introduce:

| stage | what it is | reduced by |
|---|---|---|
| `INPUT` | measurement uncertainty of what fed the model | measuring better |
| `CONDITIONING` | introduced by *preparing* data — resample, unit convert, gap fill | a better grid or honest gaps |
| `MODEL_FORM` | the model's idealisation of reality | changing the model |
| `NUMERICAL` | discretisation, timestep, solver tolerance | refining, at known cost |
| `SURROGATE` | the surrogate's departure from its training solutions | more or better training |
| `DOMAIN` | outside the validated envelope | **nothing** — it does not apply |
| `CONFIGURATION` | the model does not match the system's configuration | matching them |
| `VALIDATION` | observed disagreement with reference measurement | see §3 |

The first three of `IRREDUCIBLE_BY_MORE_DATA` matter operationally: model
form, domain and configuration all *look* like a wide interval, and the
instinct on seeing a wide interval is to collect more data. For those three
that is work with a guaranteed return of zero, and `advice()` says so.

---

## 3. Validation, and the mistake to avoid

`from_parity(predicted, observed)` turns paired samples into what they
actually established:

- **bias** — a systematic offset you may correct for
- **bias standard error** — how well the *correction* is known, `s/√n`
- **residual RMS** — what correcting cannot fix

Those last two are not the same and collapsing them is a real defect.
Averaging 161 comparisons pins the offset to about three units and does
nothing whatever to the forty units of scatter the offset does not explain.

**Validation does not add a term. It constrains the total.** A validation
stage contributes `√(u_val² − Σu_declared²)` — only the part the declared
account does not explain. See ADR-136 D9 for why adding it in quadrature
double counts.

`Pipeline.unexplained()` returns that shortfall. When it is positive, a real
error source is missing from the budget and refining the largest declared
term cannot close it.

---

## 4. Where naive propagation is wrong

Worth stating, because each of these has been shipped somewhere.

- **Correlated inputs.** Averaging readings that share a calibration.
  Handled: shared symbols do not cancel.
- **Derived channels in an independent rollup.** A derived value aggregated
  alongside its own inputs double counts. Handled: the derived quantity
  carries its inputs' symbols, so the double count is arithmetically
  impossible.
- **Nonlinear transforms.** `product` mints a residual symbol and labels the
  linearisation. Strongly nonlinear transforms want GUM-S1 Monte Carlo
  instead.
- **Unknown correlation treated as independent.** RSS is the floor only
  under non-negative correlation. `Combination` carries the premise.
- **Zero standing in for unknown.** See §2.4.
- **Validation added to the account it measures.** See §3.

---

## 5. Proof obligations

`tests/uncertainty/test_proof.py` verifies the implementation against
independent authorities rather than against itself:

- **GUM longhand.** JCGM 100 §5 implemented over the public surface only.
  Agreement to `rel=1e-12`.
- **Monte Carlo.** Sources drawn once per trial, each quantity formed as its
  own linear combination of those draws, so correlation is present by
  construction. Tolerance is *derived* from the sample size —
  `4/√(2(n−1))`, four standard errors of a standard deviation. Asserting a
  sampled quantity to `1e-6` is not rigour.
- **Property tests.** Commutativity, associativity, `correlation ∈ [−1, 1]`,
  self-correlation exactly 1, scaling preserves relative uncertainty.
- **Bracket attainability.** Both endpoints of the unknown-correlation bound
  are constructed and shown reachable, so the bound is tight rather than
  merely safe.

---

## 6. Persistence

Per ADR-136 D7, terms go in a companion table keyed to the signal row rather
than in extra columns: `GOLD_SIGNALS_BASE_COLUMNS` order is frozen and the
source set is open-ended, and one value has many sources.

`pg_upsert` merges a normalizer's row over its defaults, so an ingest that
declares `uncertainty` gets it through. The `None` default is correct: absent
is not zero.

The companion table for structured terms (symbols and coefficients, which is
what turns a bound into an exact figure) is **unbuilt**. Until it exists a
conformed row carries a magnitude without structure, so every served
aggregate over it is a bound — see §8.

---

## 8. The served boundary

`aggregate()` and `series()` mapped `mean → avg` and dropped uncertainty
entirely, so whatever an ingest declared died at the exact point a decision
consumes it. Fixed as follows.

### 8.1 It rides the envelope, not each verb

`envelope()` already carries `source`, `method` and `rows`, and it has 23
callers. Uncertainty goes there for the same reason the rest of it does:
there is one envelope and there are many consumers — CLI, MCP tools, HTTP
routes, charts, and any foreign agent reading the JSON. All of them get it
without knowing it was added.

### 8.2 Sufficient statistics, one pass, same window

The bound is reconstructed from `count`, `sum(u)`, `sum(u²)` and `max(u)`
computed in the **same statement** as the aggregate, with `FILTER` scoping
each to rows that carried a value. A window can be millions of rows and the
point of aggregating in the database is not to bring them back.

`tests/uncertainty/test_serving.py` proves the reconstruction is identical to
composing the rows through `add`/`mean` over random inputs. That equivalence
is what makes the shortcut safe rather than merely fast.

### 8.3 What each aggregate honestly supports

| `fn` | reported | why |
|---|---|---|
| `mean` | bound, scaled by the **quantified** count | dividing by the full count would be a narrower answer for less information |
| `sum` | bound | magnitudes add |
| `min` / `max` | the winning row's own uncertainty, **exactly** | an extremum is one row's reading, not a combination |
| `std` | not claimable; the measurement floor as a note | measurement error *inflates* a dispersion rather than adding to it |
| `count` | exactly zero | the one place a zero is the honest answer |

Two of those carry findings nothing reported before.

**An extremum can be undetermined.** A served peak of 91.2 ± 0.5 with forty
samples inside that interval is not the location of a peak, and a threshold
check reading it as one is acting on noise. A second pass over the identical
window counts the rivals.

**A dispersion may be entirely instrumental.** When the observed spread does
not exceed the mean measurement magnitude, nothing in the data establishes
that the process varied at all.

### 8.4 Absence is transmitted, never omitted

Every field is emitted including the nulls. `claimable: false` with a `note`
is a statement; a missing `uncertainty` key is a silence, and a reader cannot
tell a silence from "this verb forgot". The key is omitted only when the
table has no uncertainty column at all, and nothing is inferred from a
column merely being numeric — a float is not an error bar.

### 8.5 The ratchet

`test_every_aggregate_in_the_closed_set_has_an_uncertainty_rule` iterates
`AGGREGATE_FNS` and fails if any admitted aggregate has no propagation rule.
Adding an aggregate without a rule breaks the build rather than quietly
dropping uncertainty again, which is exactly how it was lost the first time.

This is the mechanism that scales. Awareness does not: a convention every
contributor must remember is a convention that decays. A closed set with a
test over it cannot.

### 8.6 Structured sources — the companion table

`silver.signal_uncertainty` is one row per (signal, source):

| column | meaning |
|---|---|
| `row_hash`, `channel` | the signal it describes (FK, `ON DELETE CASCADE`) |
| `symbol` | the namespaced source, per §2.1 |
| `coefficient` | its magnitude for this row, in the value's unit |
| `independent` | **the load-bearing field** — see below |

`independent` decides whether an error averages away, and it is the one
field here that can make a served number confidently wrong.

- `false` (**the default**) — a source **shared** across rows: a calibration
  offset, a reference junction, a common supply. Coefficients **sum**, so a
  mean keeps the whole offset. Averaging a thousand readings does not average
  away the calibration.
- `true` — a fresh **per-reading** draw: repeatability, quantisation. These
  add in quadrature and shrink as `1/√n`. Only this kind earns the averaging.

The default is `false` deliberately. A wrong `false` gives a bound that is
merely too wide; a wrong `true` gives a number that is too confident by
`√n` — at n=400 that is twenty times — and it looks entirely reasonable on a
chart. `TestGettingTheFlagBackwardsIsMeasurablyWrong` pins the factor.

**A row that declared structure does not also contribute its scalar.** The
scalar summarises the same sources, so counting both double counts. `EXISTS`
against the companion makes structured, magnitude-only and silent three
disjoint counts over one window.

**The companion is discovered, not configured.** It is `<table>_uncertainty`
and its join keys are its own columns minus `symbol`/`coefficient`/
`independent`. No conformed shape is named anywhere in `gold_query.py`, so a
new one gets a companion by following the naming.

**The divisor is every row the mean averaged**, not the subset that reported.
The served value is the mean over the whole window, so dividing the
uncertainty by a smaller count would describe a different quantity. A shared
source on `k` of `n` rows contributes `a·k/n`, because it only perturbs the
rows it applies to. The dilution that causes is stated, not hidden:
`complete: false` plus the unquantified count say that some rows contribute
an amount nothing bounds.

### 8.7 Declaring structure makes the answer narrower

The incentive has to point this way or nobody declares anything. Measured on
the live database, 600 readings sharing one bath:

| what was declared | served uncertainty |
|---|---|
| magnitude only (`0.51` per row) | `± 0.0208 .. 0.51` — a bound |
| sources (`bath 0.5` shared, `rep 0.10` per-reading) | `± 0.500017` — exact |

Same physics, same rows. The bound is honest and nearly useless; the exact
figure is honest and actionable, and it additionally says the bath is 100% of
the variance — so recalibrating is the only thing that helps, and quieting
the sensor is worth nothing.

---

## 10. What we delegate, and what we do not

The rule is that nothing proven elsewhere gets reimplemented here. Applying
it required measuring rather than assuming, and it did not land where a
reflex "use numpy" would put it.

### Delegated, after measuring

| we use | instead of | because |
|---|---|---|
| `math.hypot(*coeffs)` | `sqrt(sum(a*a))` | overflow- and underflow-safe. The hand-rolled form returns `inf` near 1e200 and `0.0` near 1e-200 — **and so does `np.linalg.norm`**, which is why the answer was the stdlib primitive and not the array library |
| `math.fsum` | `sum(...)` | correctly rounded, so a long term list cannot drift a correlation past ±1 through accumulated rounding |
| `statistics.stdev` | hand-rolled residual RMS | it *is* that quantity, and it is fsum-based. `np.std` uses pairwise summation — faster, slightly less accurate, and these arrays are small |
| `statistics.fmean` | `sum/n` | same reason |
| `numpy.random.Generator` | `random.gauss` in a loop | vectorised, so the proof suite can afford 400k draws and a tolerance derived from the sample size rather than chosen to pass |

Normalising each coefficient by its own magnitude *before* forming products
came out of the same exercise. Dividing a finished dot product by `u_a·u_b`
is algebraically identical and numerically wrong at both extremes, and it
fails **silently** — returning a plausible `0.0` for quantities that are
perfectly correlated. The property tests draw ordinary magnitudes and passed
throughout; `TestTheNumericsHoldAtTheExtremes` is what pins it.

### Not delegated, deliberately

**The affine form over globally named symbols.** The `uncertainties` package
does linear propagation with automatic correlation tracking and is the
established reference — but its correlation lives in in-process Python object
identity. It cannot survive a write to Postgres, a serialisation across MCP,
or a federated node, and surviving those is the entire requirement. There is
no library whose correlation is a *name*.

**Sufficient-statistics reconstruction.** The aggregation happens in SQL over
a window that may be millions of rows. No array library helps with data that
never comes back to the process.

**The bound under unknown correlation**, with both endpoints proved
attainable, and the three kinds of absence. These are not numerics.

### Adopted: scipy for coverage factors

`scipy>=1.11` is a declared base dependency. `axiom.uncertainty.coverage`
derives the coverage factor from the **t-distribution** at the effective
degrees of freedom rather than assuming `k = 2`.

`k = 2` is ~95% only as the degrees of freedom go to infinity. A Type A
component evaluated from `n` observations has `n − 1`:

| dof | k at 95% |
|---|---|
| 1 | 12.71 |
| 5 | 2.57 |
| 10 | 2.23 |
| 160 | 1.97 |
| ∞ | 1.96 |

At six observations, quoting `k = 2` understates the interval by 29% — which
is invisible on a chart and decisive in a limit check.

`Budget.dof` carries it, defaulting to unlimited (the GUM convention for a
Type B bound from a certificate). `Parity.dof` is `n − 1` by construction,
so `validation_stage` and `correction_stage` set it from the comparison.
`Pipeline.effective_dof()` combines them by **Welch–Satterthwaite** (GUM
Annex G) over the *reconciled* magnitudes — the same ones the interval rests
on, since a factor derived from different magnitudes would not describe it.

`Quantity.coverage(confidence=, dof=)` returns `(low, high, k)`. Prefer it to
`expanded(k)`, which takes `k` on faith. `render_budget` prints ν_eff and the
derived `k` beside the interval, because a coverage interval whose factor a
reader has to guess is not reportable.

scipy is declared rather than lazily imported: this is on the reporting path
for every served figure, and a silent fall back to the normal approximation
would be exactly the quiet overconfidence this package exists to remove.

### Worth adopting, not yet adopted

- **`uncertainties`** as a *third* verification oracle in the test suite
  (dev-only; it would never enter the runtime). Currently verified against
  hand-written GUM longhand and Monte Carlo. See §12 for why it cannot be the
  runtime carrier.
- **`MAPIE`** for conformal prediction on the surrogate link, when coverage
  validation gets built. Distribution-free finite-sample coverage is a solved
  problem and reimplementing it would be exactly the mistake this section is
  about.
- **`scipy.stats.chi2`** for the interval on the residual RMS itself, which is
  an estimate with `n−1` degrees of freedom and is currently quoted as though
  exact.

---

## 12. Crossing a boundary

`axiom.uncertainty.wire`.

### 12.1 Why no library can do this

`uncertainties` tracks correlation through **in-process Python object
identity**: two of its numbers are correlated because they hold references to
the same variable object. Write them to Postgres, serialise them over MCP, or
send them to a peer and that identity is gone — the numbers arrive
independent, and independent is the *narrow* direction.

There is no library whose correlation is a **name**. That is the whole reason
the affine form here is hand-built, and this module is what makes the name
survive the trip.

### 12.2 The collision that would make a federated answer wrong

Symbols are namespaced per extension, which is enough inside one node and not
enough between two. Two sites will both mint
`signals:tc-14:repeatability` — for **different physical sensors**.

Measured, for two sites each reporting a bath at 0.5 and a sensor at 0.1:

| | difference uncertainty |
|---|---|
| honest (qualified symbols) | **0.721 °C** |
| if the symbols collided | **0.000 °C** |

Not "somewhat too precise". A claim of *perfect* precision on a comparison
between two different sites' instruments, with a full provenance trail behind
it. A bound that is too wide is a nuisance; a bound that is too narrow is a
wrong answer wearing a receipt.

### 12.3 The form

    @site-alpha/signals:tc-14:repeatability
    @site-beta/signals:tc-14:repeatability

The origin is a principal in the platform's own `@name:context` form
(ADR-020). `check_symbol` admits bare and qualified; `_QUALIFIED` in `wire`
is built from the same pieces so the two grammars cannot drift.

- `to_wire(q, origin=)` — `origin` is **required, with no default**. A default
  is the one thing that could reintroduce the collision, because a caller who
  forgot would emit bare symbols a peer would match against its own.
- `from_wire(payload)` — **refuses** unqualified symbols rather than
  qualifying them on arrival. The receiver cannot tell "local source" from
  "forgot to qualify", and guessing local creates the false correlation.
- Re-qualifying a symbol that already carries a different origin is refused:
  it would claim someone else's measurement as your own.
- The version field is refused when unrecognised. A quantity parsed wrong is a
  wrong number, not a wrong message.

Bare symbols stay bare in storage. The companion table holds local symbols and
qualification happens at the edge, so nothing in the database needs to know
which node will read it.

### 12.4 The invariant

> **Anyone may widen. Nobody may narrow by assertion.**

Structural, not policy. A foreign contribution can only add sources, and
adding sources can only widen. Narrowing requires claiming two origins' symbols
are the same physical source — and `alias()` is the only operation that does
it, deliberately and auditably.

`alias` is occasionally correct: two sites calibrated against the same national
standard genuinely share it, so a difference between them really is tighter
(0.721 → 0.141 °C in the case above) and refusing to say so overstates. But the
claim is made by somebody who knows, never inferred from two names matching.
Aliasing onto a bare symbol is refused, since a canonical shared source must
say whose it is or it collides with a local one.

---

## 14. Is any of it populated?

`data.uncertainty_coverage` (a verb; also on the MCP read surface).

The apparatus in §§6–8 can be complete and correct while **nothing upstream
declares anything**. In that state every aggregate returns
`claimable: false`, no check fails, and no alert fires. That is the
channel-map failure one level down — a map can be complete and match
nothing — so coverage is a surface rather than something a reader goes
counting for themselves.

### 14.1 The state today

Measured, not assumed: **no normalizer emits `uncertainty` or
`uncertainty_terms`.** Not one, across every example and source in the tree.
The write seam accepts them and nothing produces them.

So the honest production reading of every served figure is "no uncertainty
reported", and the verdict says exactly that rather than dressing it up as
a percentage:

> none of 300 served point(s) carries an uncertainty, so every aggregate
> over them reports none. Nothing is broken and nothing will report an
> error — no ingest has been told what its instruments are worth.

Which instrument shares which calibration bath is **per-site channel-map
data, not platform code**. The platform's job is to make the absence
visible, and that is what this is.

### 14.2 What it reports

`gold.uncertainty_coverage`, per (site, stream), three **disjoint**
populations — ADR-136 D5's three kinds of absence, counted over real rows:

| population | meaning | aggregate behaviour |
|---|---|---|
| **exact** | sources declared | an exact figure |
| **bounded** | magnitude only | a bound with a stated premise |
| **unclaimable** | neither | claims nothing |

A row carrying structure also carries a summary scalar, so it is counted
once — in `structured` — and `magnitude_only` subtracts it. Otherwise the
fractions sum past one.

`gold.uncertainty_sources` is the inventory: which sources exist, the
coefficient range, and **whether each is shared or per-reading**. That last
column is why the inventory exists. A source declared per-reading when it is
actually shared yields a figure too confident by `√n`, and nothing
downstream can tell — so a human has to be able to look at the declaration
and say so.

Live, on the development database:

```
  site/stream                    points    exact  bounded   silent
  uncertainty-demo/loop            1350     1200      100       50   mixed
  silent-site/loop                  300        0        0      300   UNCLAIMABLE

  symbol                                    kind          points  coefficient
  signals:cal-bath-a:offset                 SHARED          1200          0.5
  signals:tc-14:repeatability               per-reading      600          0.1
  signals:tc-15:repeatability               per-reading      600         0.12
```

### 14.3 Why a view and not a per-row check

A per-row `EXISTS` against the companion over a partitioned signals table is
not something a dashboard can call. The view pre-aggregates the companion to
one row per signal **before** the join, because a plain `LEFT JOIN` turns one
signal with three sources into three rows and inflates `points` by the source
count. A test asserts both.

---

## 16. What makes the contract binding

§2 states the contract. A contract nothing checks is a suggestion, and the
specific failure being guarded is invisible by construction: an extension
produces values with no uncertainty, every aggregate reports
`claimable: false`, and **nothing anywhere fails**.

Four enforcement points, at four different moments.

### 16.1 Creation — the scaffold

`axi ext init` emits `[extension.uncertainty] posture = "not-applicable"`,
with the four values and their consequences in the comment. A fresh
extension produces nothing, so `not-applicable` is correct; the comment says
to change it the moment it emits a value. A rule an author meets only after
failing lint is a rule they meet too late.

### 16.2 Declaration — `axi ext lint`

| code | severity | when |
|---|---|---|
| **AEOS090** | error | the posture is outside the closed vocabulary |
| **AEOS091** | warning | a value-producing capability (`normalizer`, `emitter`) declares no posture |

`AEOS090` is an error because a declared surface that is *misspelled* is
worse than one that is missing — a reader believes it. `AEOS091` is a warning
because the check is new and no extension in the fleet has declared one yet:
erroring would fail lint everywhere at once and teach people to pass
`--no-verify`. The ratchet, not the cliff.

The field is also in the AEOS JSON Schema, which has
`additionalProperties: false` on the Extension block — so a field the lint
requires and the schema rejects would be a rule that cannot be obeyed. A test
asserts the two copies of the vocabulary agree, because two copies drift and
then a manifest passes one gate and fails the other with nobody able to say
which is right.

`data_platform` declares `posture = "carries"` itself. A field the platform
requires of others and does not set is the defect class this repo calls a
declared surface that does not exist.

Note what the posture does **not** say. `carries` is a statement about the
CODE — that structured sources are supported and composed exactly when
present. It is not a claim about the data. `data.uncertainty_coverage` (§14)
answers the question the field cannot.

### 16.3 Authoring — `data.conform_try`

The dry-run loop is the only place an author finds out what their row will be
able to say, so it now reports per row:

- declares nothing → *"every served aggregate over it will report none and no
  check will fail"*
- a magnitude without sources → *"aggregates can only be BOUNDED … declaring
  more makes the answer narrower"*
- `uncertainty: 0` → *"a claim of PERFECT precision … NULL and zero are
  different facts"*
- sources declared → which are **shared** (will not average away) and which
  are **per-reading** (will), with the consequence of getting the flag
  backwards quantified: *at 400 readings, 20× too confident*
- a scalar that disagrees with its own terms, a malformed symbol, a symbol
  with no coefficient

Notes, not errors. Declaring nothing is a legitimate state; not knowing that
you declared nothing is not.

### 16.4 The worked pattern

`conformance/examples/reading_with_uncertainty.py` — the composable
counterpart to `one_reading`. It fixes the **shape**: the symbol grammar, the
two kinds, and a budget that names its measurand and carries its degrees of
freedom.

It invents **no coefficients**. Every magnitude is read off the record,
because which instrument shares which calibration standard is the site's own
knowledge and belongs in its channel map as data. A platform example shipping
plausible-looking numbers would be worse than one inventing nothing, because
somebody would copy them. A test asserts no coefficient is hardcoded.

### 16.5 Consumption — the MCP handshake

`SERVER_INSTRUCTIONS` is the one string every peer harness reads on connect,
and this failure is a *model* failure rather than a code one: the envelope
carries the uncertainty faithfully and an agent that relays the value alone
has published a more confident claim than the platform made. So the
instructions say: report it with the value, never the value alone;
`claimable: false` means nothing reported one, not zero; quote a range with
its premise rather than picking an end; and

> **do not compute or combine uncertainties yourself.**

Asked to combine two, a model produces a plausible wrong number — correlation
is not visible in the magnitudes. The arithmetic stays in
`axiom.uncertainty`; the agent relays what it returns.

### 16.6 What is still not enforced

The declaration is about the extension, not the row. Nothing stops a
`carries` extension emitting rows that declare nothing — that is what §14's
coverage surface measures rather than prevents, and preventing it would mean
refusing data because its instrument is uncharacterised, which is worse than
serving it honestly labelled.

---

## 18. Where it meets the bronze→silver toolkit

The uncertainty surface and the conformance toolkit have to agree, and three
places where they did not are worth recording because each was invisible.

### 18.1 `uncertainty_terms` needs no plumbing

`conform_rows` copies each normalizer row (`dict(sig)`) and hands it to
`upsert`, so an unknown key survives. `pg_upsert` merges `**row` over its
defaults and `_write_terms` picks the key up. A normalizer that declares
terms reaches `silver.signal_uncertainty` with nothing in between changed.

### 18.2 A withheld value is not a measurement, and not a missing field

A device-asserted fault is **withheld**, not served: the fault travels as
`quality` and the reading is NULL, so SQL aggregates self-correct. `avg()`
ignores a NULL, where a sentinel inside an average is silently wrong.

That has consequences in two directions the toolkit had backwards.

**The coverage view counted rows, not values.** `count(*)` reported a
withheld row as a point — an *uncharacterised measurement*, which is a third
thing it is not: it is not a measurement at all. Now `count(s.value)`, which
also makes the view agree with the serving path, since `_structured_terms`
already scopes every statistic to `value IS NOT NULL`. **Two denominators
for one question would make the coverage figure disagree with the aggregate
it describes.**

**`conform_try` called a correct withholding a missing field.** That is the
one loop telling a normalizer author what their rows will do, and it would
have taught them to emit the sentinel instead — producing exactly the defect
the withholding prevents. `WITHHELD_QUALITIES` now admits a NULL value under
a quality that says the reading is bad, and `good`/`ok` are deliberately
absent: a NULL under a quality claiming the reading is fine is a real defect,
because something produced nothing and said nothing was wrong.

It also reports the **opposite** direction, which is the one that reaches a
served surface: a row claiming `quality = "bad"` while still carrying the
number. Downstream cannot know that number is a fault code.

And a withheld row is no longer nagged about uncertainty — it has nothing to
be uncertain *about*, so the notes were noise on the rows already saying
something.

### 18.3 Measured on the live database

50 rows, 10 with the value withheld on a fault:

```
coverage : points=40  exact=40 bounded=0 silent=0
aggregate: rows=40    value=531.50  u=0.5  quantified=40 unquantified=0
denominators agree: True (both 40, not 50)

quality=bad   rows= 10  carrying a value=  0     <- the fault TRAVELS
quality=ok    rows= 40  carrying a value= 40
```

The mean is over the 40 real readings, and the shared encoder offset stays
exactly 0.5 rather than averaging down.

### 18.4 Still not dovetailed

- `preview.py` reports `missing_units` before an ingest runs and says nothing
  about uncertainty. The same pre-flight question applies.
- `conform_verdict`'s strict mode does not consider withheld provisional
  sources, so a run can be green while every declared uncertainty is gated.
- `silver.signals` has no `raw_value` or `quality_reason`. ADR-132 proposes
  both; until they land, a withheld reading's own number is lost and the
  producer's diagnosis has nowhere to go.

---

## 19. Reporting

`render_budget(pipeline, value=, unit=)` produces the budget GUM §7 asks
for: one row per source with its stage, magnitude, variance share and
evaluation type; the validation constraint shown separately and explicitly
*not* summed with the rows; the combined and expanded uncertainty; the
coverage interval; where the next hour goes; and every term that failed to
state what it is valid over.

A combined uncertainty published without its budget cannot be checked,
cannot be reproduced and cannot be argued with, which is why the standard
asks for the table and not the number.
