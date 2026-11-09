# The acceptance harness

`axi app acceptance` answers "how far is the MVP?": every acceptance row in the web app
build-out spec, the tests that carry its ID, and a verdict.

```bash
axi app acceptance --phase P1            # the P1 rows, from the last run's results
axi app acceptance --phase P0 --run      # run the tagged tests first, then report
axi app acceptance --phase P1 --strict   # exit 1 unless every row passes (a gate)
axi app acceptance --json
```

The same report is the `app.acceptance` skill, so chat and MCP clients
(`axiom_app__acceptance`) get the same answer. Running tests stays on the CLI.

## A row is proven only by a test carrying its ID

| Language | Form |
|---|---|
| Python | `@pytest.mark.acceptance("MVP-02-A1")` on the test, its class, or the module (`pytestmark`); several IDs may be listed |
| Python | a test named `test_mvp_02_a1_…` (also `test_var_a3_…`, `test_p0_acc_a1_…`) |
| TypeScript, JavaScript | the title starts with the ID: `it("MVP-02-A1 …")`, `test(…)`, `describe(…)` |

The verdict follows results, not tags:

- **pass**: every tagged test ran and passed;
- **fail**: any tagged test failed;
- **unproven**: no test carries the ID, or a tagged test has not run or was skipped.

The rows come from the spec's tables, so the spec is the registry: rows above its
`## P1` heading are P0, the rest P1. A duplicate ID is refused, and a test tagged with
an ID the spec does not have is listed as an orphan.

## Across repositories

Without a configuration, the harness reads this repository only. A workspace names
its repositories in `acceptance.toml` at its root (found by walking up from the
working directory), or in a file named by `$AXIOM_ACCEPTANCE_CONFIG`. Paths are
relative to the file.

```toml
[acceptance]
spec = "axiom/docs/specs/spec-webapp-buildout-mvp.md"

[[repo]]
name = "axiom"
path = "axiom"
lang = "python"
include = ["src", "tests"]

[[repo]]
name = "appkit"
path = "axiom-appkit"
lang = "js"
include = ["frontend/src"]
results = "frontend/acceptance-results.json"   # vitest --reporter=json --outputFile=…
run = ["sh", "-c", "cd frontend && npx vitest run --reporter=json --outputFile=acceptance-results.json"]

[[repo]]
name = "product"
path = "product"
lang = "python"
include = ["src", "tests"]
python = ".venv/bin/python"
```

`--run` runs a Python repo's tagged node IDs only (with its `python`, else this
interpreter) and a JS repo's `run` command, then caches the results under the state
directory. A repo's `results` file (JUnit XML or vitest JSON) is read on every report,
so CI can publish results without a local run. A repository that is not checked out
is reported, and the report still answers for the rest.
