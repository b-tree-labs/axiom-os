# RIVET playbook — production job failures and floating dependencies

Plain-language, repo-agnostic. Worked example at the end
(SoilMetrix-Inc/soilmetrix-platform, Oct 25–29 2026).

## 1. Detect floating-dependency risk

Flag a service as **floating** when any of these hold:

- No lock file next to the dependency manifest (no `uv.lock`, `poetry.lock`,
  `requirements.lock`/hashed `requirements.txt`, `Pipfile.lock`, etc.).
- Dependencies use ranges or no versions (`>=`, `~=`, bare names).
- The base image is a moving tag (`python:3.12-slim`) rather than a digest
  (`python:3.12-slim@sha256:...`).

Flag a pull request as **high risk** when it bumps the base image or the
Python version and changes nothing else (typical Dependabot shape). On a
floating service such a PR silently re-resolves every dependency. Comment on it:
"base/Python bump on an unlocked service — every transitive dep will move;
needs a lock file or a resolved-deps diff before merge." Never auto-land it.

## 2. Root-cause protocol for a production job failure

Do these in order. Do not name a cause before step 4.

1. **First failure time.** From the platform logs (Cloud Run, k8s, Actions),
   find the earliest failing run and its exact timestamp and image digest.
   Also find the last good run and its digest.
2. **What changed before it.** List image builds/deploys and merges to the
   default branch in the window between last good and first bad. Use merge
   and deploy timestamps, not PR open dates.
3. **Diff resolved deps.** `pip freeze` (or equivalent) inside the last good
   and first bad images; diff them. Include the Python version and base image.
4. **Match the error to the diff.** The cause is the change that is both in
   the window and explains the error text.
5. **Never blame a PR by its title alone.** A PR whose title matches the
   symptom is a hypothesis; confirm it merged and deployed *before* the first
   failure. If it landed after, it is a response, not the cause.

## 3. Fix-deployed verification (before any rerun)

Before rerunning failed jobs, confirm:

- The fix PR is merged, and note its merge commit SHA.
- An image was built from that commit (or a descendant) — check the build's
  source SHA.
- The job/service is actually pinned to that image digest now (inspect the
  live revision, not the deploy workflow's success).
- Record the "fix live at" time. Reruns before it are expected to fail and
  are not evidence the fix is wrong.

## 4. Lock-file update protocol

1. Regenerate the lock file with the target Python and base image.
2. Review the **transitive** diff, not just direct deps. Call out major
   bumps and default-driver/backend changes.
3. Run tests, including an **import test** that imports every entry module
   and driver the service uses in production.
4. Smoke-run one real job against the new image.
5. Deploy; watch the first scheduled runs.
6. Roll back by **image digest** (not tag) if anything regresses.

Where a repo has a per-service doc (e.g.
`services/lakehouse/DEPENDENCIES.md` in soilmetrix-platform), follow it and
keep it current.

## 5. Guard: fix PR that doesn't take effect

Before declaring a fix PR done:

- Show that the changed code path is actually executed in production
  (the module is imported, the function is called, the config key is read,
  the patch is applied before the code it patches runs).
- Prefer a test that fails without the fix and passes with it, run against
  the built image.
- After deploy, confirm the original error signature disappears from logs.
  If it persists with the fix live, the fix did not take effect — reopen.

## Worked example — SoilMetrix lakehouse, Oct 2026 (times CT)

- **Oct 25:** Dependabot bumped the lakehouse image `python:3.10-slim` →
  `3.12-slim`, nothing else. No lock file, so deps floated.
- SQLAlchemy 2.1.6 now defaulted to psycopg 3, which wasn't installed.
  **From early Oct 28** field-compute on Cloud Run crashed with
  `ModuleNotFoundError`.
- **#2765** added psycopg 3 — correct, and it surfaced the next layer: a
  dagster-postgres `NOTIFY $1` server-side binding error.
- **#2817** rewrote to `pg_notify`, but the rewrite never ran (step 5 guard
  would have caught it).
- **#2845** applied it correctly; merged Oct 29 9:51 AM, image live 9:58 AM.
  Reruns before 9:58 failed; after, succeeded (step 3).
- **Mistake to avoid:** an assistant blamed #2765 from its title without the
  timeline. Step 2 shows the first failure predates it; the cause was the
  unlocked base-image bump (step 1).
