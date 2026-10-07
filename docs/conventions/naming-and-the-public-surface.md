# Naming and the public surface

Axiom is domain-agnostic platform code and it publishes to a public mirror.
Three different things get confused when people ask "can I write that here?",
and the answer is different for each.

## The three categories

### 1. Institution identifiers — never in Axiom

A name that identifies **an organisation**: the university, a partner
university, a company, a funder.

Forbidden because Axiom is a product other institutions install. A tenant
reading their own platform's source should not find another tenant's name in
it, and our own name in the public surface turns a general-purpose platform
into one lab's tooling.

    UT, utexas, The University of Texas
    ANDRETTI            # the Nuclear Engineering Teaching Laboratory IS UT's lab,
                    # so naming it identifies UT by another route
    SENNA, PROST, FANGIO  # partner institutions
    TACC

### 2. Facility and deployment identifiers — never in Axiom

A name that identifies **a specific installation**: a host, a node, a site id,
a person.

    rascal                  # host codename
    10.159.x.x              # internal subnet
    senna-loop            # a partner's site id
    @sam:andretti               # a real colleague, in a test fixture
    /Users/<name>           # personal filesystem paths

A real person's name in a fixture is separately wrong regardless of the mirror:
see the no-real-names-in-fixtures rule.

### 3. Domain vocabulary — allowed, and correct to use

The engineering vocabulary of the field, including **equipment types**.

    reactor, nuclear, criticality, flux, rod, fuel temperature
    TRIGA   # Teaching, Research, Isotopic, General Atomics — a reactor TYPE,
            # like PWR or BWR or MSR. Hundreds exist worldwide. Naming the type
            # is engineering vocabulary, not an identifier.
    PWR, BWR, MSR, EPICS, Modbus

Axiom being domain-agnostic does not mean domain-illiterate. A data platform
built for high-consequence operations may say "reactor" in an example, and a
DAQ contract may name EPICS. The line is **identity, not topic**.

> **The test, in one question:** does this name tell a reader *who* or *where*?
> If yes it is category 1 or 2 and it does not belong in Axiom. If it only tells
> them *what kind of thing*, it is category 3 and it is fine.

## What to write instead

**Placeholder sites are named after racing drivers.** Instantly recognisable
as stand-ins, unrelated to any real deployment, and — the reason this
convention took four attempts — from a vocabulary that shares nothing with the
domain.

Each rejected attempt was rejected for a real reason, recorded here so nobody
rediscovers them:

* **Not radiation types.** `alpha`, `beta`, `gamma` read as a measurement, not
  a placeholder, in a codebase full of measurements.
* **Not SI units.** `silver.signals` has a `unit` column, so a site called
  `joule` or `tesla` beside `unit='joule'` is a worse collision than the one it
  replaces. That rules out most eponymous physicists: Newton, Joule, Watt,
  Ampere, Volt, Ohm, Hertz, Kelvin, Pascal, Tesla, Weber, Henry, Farad,
  Siemens, Coulomb, and Curie / Becquerel / Gray / Sievert, which are units
  *and* nuclear.
* **Not national laboratories.** Squarely in the domain, and worse, some are
  real collaborators: INL and ORNL both appear in live work, so naming either
  implies a relationship the code should not assert.
* **Not physicists generally.** Even the non-eponymous ones sit close enough to
  the subject matter to read as content rather than placeholder.

| role | use |
|---|---|
| first placeholder site | `senna` |
| second | `prost` |
| third | `fangio` |
| fourth | `lauda` |
| the host lab (stand-in for "our own") | `andretti` |

    site:senna-loop        @operator:senna        platform:andretti

**Check a candidate before adopting it.** Every name above was grepped against
the whole tree and returns zero existing occurrences; `hamilton` was dropped for
returning 196. A placeholder that already means something else is not a
placeholder.

| instead of | write |
|---|---|
| `site:senna-loop` | `site:senna-loop` |
| `@sam:andretti` | `@operator:senna` |
| "the ANDRETTI TRIGA" | "the reactor", or "a TRIGA-type reactor" when the type matters |
| `rascal` | `node-1`, `the canary node` |
| a real Box folder id | a sentinel such as `000000000000` |

Site ids in fixtures should be obviously synthetic. `senna-loop` cannot be
mistaken for one of our deployments; `senna-loop` can, and has been.

## Where the domain names DO belong

NeutronOS. That is the whole point of the split: Axiom ships the mechanism,
NeutronOS ships the nuclear domain, and a site repository ships one
deployment's configuration.

    Axiom        conform, DAQ contract, ingest face, authz model   generic
    NeutronOS    TRIGA console schemas, rod scenarios, ROM streams  domain
    site repo    this node's pins, this site's connectors           deployment

A normalizer that knows a reactor's schema belongs in NeutronOS and reaches
Axiom through the `axiom.data_platform.normalizers` entry point. A landing
table that only a nuclear package writes belongs to that package and reaches
the platform audit through `axiom.data_platform.landing_tables`. Both exist so
domain knowledge composes in without the platform naming it.

## Enforcement

`scripts/build_public_mirror.py` carries `FORBIDDEN_TERMS` and
`tests/test_mirror.py` fails the build on a hit. The list is category 1 and 2
only — adding category 3 vocabulary would make the guard fire on correct code
and train people to allowlist their way past it.

Where a term legitimately must appear (this file, the mirror script itself, the
ADR that explains the split) the path is allowlisted rather than the term
weakened.
