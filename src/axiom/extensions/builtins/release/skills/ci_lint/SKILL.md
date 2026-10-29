# ci_lint

Warn on workflow shapes that waste CI minutes. A warning, never a blocker;
`land` prints the same findings before it acts.

Rules: `pr-concurrency`, `fast-stage`, `docs-filter`, `nightly-matrix`,
`strict-without-queue` (needs `repo`, reads branch protection and rules).

## Inputs

- `path` — workflow directory. Default `.github/workflows`.
- `repo` — also check this repo's default-branch protection.
- `format` — `text` or `json`.
