# Contributing to Axiom

Thanks for your interest in Axiom — a domain-agnostic platform for building
federated, AI-powered operational systems. This guide gets you from clone to
merged PR.

By participating you agree to our [Code of Conduct](CODE_OF_CONDUCT.md).

> **Heads-up: this repository is a public mirror.** Development happens in a
> private source-of-truth repo and is synced here, so **a pull request opened
> directly against this repo may be overwritten by the next sync.** While we
> bootstrap the external-contribution flow, please **propose changes via an
> [Issue](https://github.com/b-tree-labs/axiom-os/issues) or
> [Discussion](https://github.com/b-tree-labs/axiom-os/discussions) first** —
> a maintainer will coordinate landing accepted changes. Bug reports, feature
> requests, and questions are always welcome here.

## TL;DR

```bash
git clone https://github.com/b-tree-labs/axiom-os.git
cd axiom-os
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"

pytest                          # tests must pass (and you write them first — see below)
ruff check src/ tests/          # lint
```

Then branch, commit with a `Signed-off-by:` line (`git commit -s`), open a PR.

## Ways to contribute

- **Report a bug** or **request a feature** — open an issue using the templates.
- **Fix or build** — grab an issue (comment so we don't double up) or propose
  something new in an issue first if it's substantial.
- **Write an extension** — Axiom is extensible by design; see *Building an
  extension* below. You don't need to touch core to add capabilities.
- **Improve docs** — README, specs, ADRs, docstrings all welcome.

## Development setup

- **Python ≥ 3.11** (CI runs 3.11–3.13).
- One virtualenv, editable install: `pip install -e ".[all]"`.
- The CLI is `axi` (alias `axiom`). `axi config` provisions a local model so you
  can run without a cloud key; `axi status` shows platform health.

## House rules

These are the conventions the codebase is built on. PRs are reviewed against them.

1. **Tests first (TDD).** Write the failing test, then the implementation. Every
   behavior change ships with a test. CLI verbs need a subprocess-level test that
   runs `python -m ...` and asserts on stdout.
2. **Everything non-core is an extension (AEOS).** Capabilities live in
   `src/axiom/extensions/builtins/<name>/` with an `axiom-extension.toml`
   manifest. Core primitives (memory, identity, federation, policy) live in
   `src/axiom/<module>/`. See [the AEOS spec](docs/specs/spec-aeos-0.1.md).
3. **CLI verbs are thin wrappers over skill functions** (`(params, ctx) ->
   SkillResult`, ADR-056) — the same capability is then callable from the CLI,
   from a peer agent over A2A, and from an external harness over MCP.
4. **Stay domain-agnostic.** Axiom is the substrate domain products build on — it
   must not hardcode any specific institution, deployment, or vertical (no
   `nuclear` / `reactor` / a named consumer / a specific org or host baked into
   platform code). Use labeled generic examples. This is enforced:
   `tests/test_mirror.py` fails CI if a public file leaks a forbidden
   institution/consumer/personal/credential term (see `scripts/build_public_mirror.py`).
5. **Database access** goes through `axiom.infra.db.session_for("<ext>")`
   (schema-per-extension, ADR-052) — never construct your own engine or write to
   `public`.
6. **No secrets, PII, or personal paths** in code, tests, or docs. Use
   `user@example.org`, `/Users/example/...`, etc.

## AI-assisted contributions

AI coding tools are welcome — this project is built with them. But the bar for a
contribution is the same whether a human or a model wrote it, and **you are
accountable for what you submit**:

- **Understand your change.** If a reviewer asks "why this approach?", you should
  be able to answer. Don't open PRs you can't explain.
- **Run it.** Tests pass locally, the code actually works — not just "looks
  plausible." Models hallucinate APIs and tests; verify before you push.
- **Keep it focused and human-scale.** No bulk, auto-generated, or drive-by PRs
  (e.g. mass "fixes" across the repo from a script or agent). One real change per
  PR. Low-effort or unverified AI output will be closed without extensive review.
- **Disclose meaningful AI assistance** in the PR description. We don't mind it;
  it helps reviewers calibrate.
- **Don't paste others' secrets or copyrighted code** that a model surfaced. Your
  DCO sign-off certifies you have the right to contribute the code.

Good AI-assisted PRs are great. Unreviewed model output that wastes maintainer
time is the fastest way to get blocked.

## Scope, expectations & saying no

- **This is volunteer-maintained.** Reviews and replies are best-effort, not
  SLA-backed. A nudge after ~2 weeks is fine; pinging maintainers privately for
  free support is not (see [SUPPORT.md](SUPPORT.md)).
- **Propose before you build something big.** Open an issue for substantial
  changes so we can agree on the approach before you invest time. Prefer an
  **extension** over a core change wherever possible.
- **We may say no.** A change can be good and still not fit Axiom's scope or
  direction (see [GOVERNANCE.md](GOVERNANCE.md)). We'll explain why. Keeping the
  core small and domain-agnostic is a feature.

## Branches, commits, and PRs

- Branch from `main`; keep PRs focused (one concern).
- **Sign off every commit** with the [Developer Certificate of Origin](https://developercertificate.org/):
  `git commit -s` adds the required `Signed-off-by: Your Name <you@example.com>`
  trailer. PRs without it won't pass.
- Write clear commit messages: a concise subject line, then *why* in the body.
- Open a PR against `main`; fill in the template. CI (lint, unit tests on
  3.11–3.13, wheel build, the mirror guard) must be green.

## Building an extension

```bash
axi ext init my-extension        # scaffold an AEOS-conformant extension
# ... implement skills + CLI verbs ...
axi ext lint                     # check conformance
pytest src/axiom/extensions/builtins/my_extension/tests/
```

An extension declares what it provides (cmd / agent / tool / service / adapter /
skill / hook) in `axiom-extension.toml`; the CLI, MCP catalog, and agents
discover it from the manifest. Extensions can also ship as separate PyPI packages
and self-register via entry-points — no core changes required.

## Where to file issues

This public repository is the home for **community bug reports and feature
requests** — use the issue templates here, and ask open-ended questions in
[Discussions](https://github.com/b-tree-labs/axiom-os/discussions). Rule of
thumb: *if a stranger could have hit it, it belongs here.*

Day-to-day implementation planning, roadmap, and anything domain- or
business-specific is tracked privately by the maintainers; when a public issue
needs work, a maintainer links it to the private tracker. So:

- **Bug / feature / question** → this repo (issue or discussion).
- **Security vulnerability** → privately, never a public issue (see below).

## Reporting security issues

Please **do not** open a public issue for vulnerabilities. See
[SECURITY.md](SECURITY.md) for private reporting.

## License & DCO

Axiom is licensed under [Apache-2.0](LICENSE). By contributing, you agree your
contributions are licensed under Apache-2.0, and you certify the DCO via your
`Signed-off-by` line. New files carry the standard SPDX + copyright header used
across the tree.

Questions? Open a [discussion or issue](https://github.com/b-tree-labs/axiom-os/issues).
We're glad you're here.
