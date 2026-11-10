# Implement a design

For a developer, or a developer's coding agent, who has a ticket with a design attached. Read this before you touch a screen. The appkit repository holds the tools named here, and each repository's agent instructions point to this guide.

## Where a design lives

The master design is a hosted page that most coding agents cannot open, so do not try to fetch it. Each repository holds a copy of what it needs, and `docs/design/manifest.json` records which master version that copy is.

- Generic screens live in appkit under `docs/design/`.
- A product repository holds only its own product sections.
- A site repository holds only its branded references.
- A PNG is held in one repository. A ticket cites it with the repository name and its path, for example appkit `docs/design/snapshot-2026-11-06/shots/entry-detail.png`.
- A ticket may name a screen as `DESIGN:<screen>`. Look the name up in `screens` in that repository's manifest.

Never edit a design copy by hand. Run `python scripts/design_copy.py check` to verify a copy. Only `refresh`, run from a downloaded master, changes one.

## The order of work

1. Read the ticket, then the design README in the repository where the change lands.
2. Open the image. Then read the code it describes.
3. Find the component that already does this and extend it. Do not write a second one.
4. Write the failing test first. Watch it fail for the reason you expect.
5. Build the smallest change that passes.
6. Compare the screen with its design, as below.
7. In the pull request, cite the design file and the acceptance ids, and attach the comparison report.

## What wins when two sources disagree

- A written spec and the design README beat a mockup.
- Snapshots can carry older chrome, buttons or colours. The snapshot's README says which parts are current.
- Words in a mockup are fixture data unless the ticket freezes them. Do not copy names or values into code.
- An image is for checking your work. When you move a component between repositories, read its source and move that; never rebuild it from a picture.
- If the design and the spec truly conflict, stop and ask. Do not pick one quietly.

## Which layer does it belong in

| Change | Repository |
|---|---|
| A generic component, token, page frame | appkit |
| A generic server capability | Axiom |
| A screen or workflow for one product | that product's repository |
| Brand, labels, flags, group documents | the site repository, configuration only |

Generic repositories name no domain, product or site. In Axiom, run `python -m pytest tests/test_axiom_names_no_domain.py -q` before you push.

## Compare the screen with its design

From an appkit clone:

```sh
npm --prefix tools/visual run visual -- --screen <name>
```

It renders the screen in the light, dark and console themes and at phone width. It exits 1 when a variant differs or has no reference, and writes a side-by-side report. Setup and how to add a screen are in appkit's `tools/visual/README.md`.

If your screen is not listed yet, add it. Do not loosen `maxDiffRatio` to make a run pass. If a built screen and its image should differ, say why in the pull request and have a person agree.

## The traps

These have each cost real time.

**The bundle fingerprint.** In appkit, every file under `frontend/` counts, including `package.json`, the lockfile and the preview pages. After any change there, run `cd frontend && npm run build`. It re-stamps `src/axiom_appkit/static/BUILT_FROM`. Commit the rebuilt bundle and the stamp together, or the staleness test in `test_the_components_ship.py` fails. Tests are excluded from the fingerprint.

**Generated files.** `src/axiom_appkit/tokens.css` is generated from `frontend/src/tokens.css`. Edit the source and run `scripts/gen_served_tokens.py`. Never edit the served copy.

**The design scale.** Write a token, never a literal. A radius, type size, shadow or colour written directly fails `test_design_scale.py`.

**Provenance headers.** A new component file under `frontend/src` needs an `Extracted-From:` or `Extraction-Exempt:` header. Never regenerate `provenance-baseline.json` to get past `lint:provenance`.

**One of each.** There is one button style (`axk-btn`, specified in `docs/design/buttons.md`), one signing card, one chat input and one status mark: a glyph and a word, never colour alone. Reuse them.

**Hooks.** A pre-push hook may repair a file, commit the repair and stop. That is not a failure: read what it did and run `git push` again. Never use `--no-verify` to get past one. If the hook reports that `main` is red, stop and report it.

**Staging.** Name each file you add. `git add -A` sweeps in reports, local images and installed packages.

**Stale code.** A page can keep showing old behaviour after a correct fix. Check which build is being served before you change the code again.

## Before you ask for review

- The tests pass, and you watched each new one fail first.
- The comparison report is attached, for every theme and for phone.
- Every state the ticket lists is shown: empty, loading, stale, error and not permitted, as well as the populated one.
- The diff holds only what the ticket asked for.
