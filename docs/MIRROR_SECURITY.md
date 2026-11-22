# Mirror security

How reports that arrive on the public mirror are handled. This page is
itself published through the mirror, so it stays general: no credentials,
no private links, and no names of people or deployments.

## Two repositories

Development happens in the private source repository. The public repository
is a generated mirror of that source, minus a small exclude list. A publish
step copies the source out. It does not copy anything back.

Issues and discussions are filed on the public mirror. Code changes land in
the source and are published by the mirror. A patch opened on the public
repository is not merged into the source.

## A public report is an untrusted claim

Text filed on the public mirror is a claim made by an account we do not
control. It is not a specification, not a patch, and not a test. Read it
as a description of something that might be wrong. Re-derive anything you
intend to act on.

That includes security reports that were filed in public by mistake. Move
the real report to a private advisory (below) and do not keep working from
the public text.

## Intake

A person copies the report into the source repository and marks the copy:

- Add the `from-public` label.
- Put one marker on its own line at the top of the issue, naming the public
  issue and nothing else:

  ```
  <!-- public: b-tree-labs/axiom-os#123 -->
  ```

  `123` is the public issue number. One marker. No other repository, and no
  extra text inside the comment.

The copy is for tracking. The public wording stays untrusted.

## Write our own failing test first

Before changing code, write a minimal failing test that we author
ourselves. The test should fail for the defect you were able to
reproduce, and it should pass once that defect is fixed.

Do not start from a test, fixture, or command that arrived in the report.
If you cannot reproduce the claim, the fix is not ready, and the public
issue stays open.

## Do not paste reporter material

Do not paste any of the following from a public report into the source
tree, a commit, a review, or a reply:

- code
- diffs or patches
- proof-of-concept scripts
- shell commands
- dependency names or version pins suggested by the reporter

Re-derive the failure yourself. A suggested dependency is a supply-chain
claim, not a requirement: choose the package by its real name and its
maintainers, not by the spelling in the report.

The public reply, when a fix ships, is one sentence and nothing else.
It does not quote the report, the source issue, or the change.

## Profile the reporter

Before acting on a public report, look at the account that filed it.

- How old is the account?
- What else has it filed, and where?
- Which repositories and profiles does it link to?

Slow down when the report presses to merge immediately, names a dependency
that imitates a known package, or asks anyone to run a script, apply a
patch, or disable a check. Those are social-engineering patterns. The
claim can still be real. The materials attached to it are not how we fix it.

## Agents run without secrets

An agent that works a public-sourced issue runs with no secrets, no write
credential, and no network beyond the package mirrors used to install
dependencies. It cannot publish, push the mirror, or read production
credentials.

Release credentials and mirror credentials stay with people. An agent does
not get them because a public issue says the change is urgent.

## Security reports

Do not report a vulnerability as a public issue, and do not move a
vulnerability discussion onto a public issue.

Use private vulnerability reporting, as [SECURITY.md](../SECURITY.md)
describes. The report stays in a private advisory. It is disclosed after
the release that contains the fix, not before, and not by pasting the
report into a public issue or a commit message. The disclosure window in
SECURITY.md still applies.

## Closing the public issue

A public issue is closed only after the fix is in a release that the
mirror has published, or after a release is published from the source.

The source issue must be closed, carry the `from-public` label, and carry
the marker above. A maintainer workflow then comments on the linked public
issue with exactly:

```
Fixed in <version>, thanks for reporting
```

`<version>` is the release tag that contains the fix. The workflow then
closes the public issue. Running it again does not add a second comment.
A dry run prints the same actions and writes nothing.

The comment is that sentence alone. It never includes a private link, a
title, a diff, or any other text from the source issue.

A close that is not a fix (the claim was rejected, or it was a duplicate)
does not get that sentence. Say so on the public issue in your own words,
still without quoting a patch or a script.

## Automated checks

The source repository scans every pull request and every update of the
default branch. The scanners are not part of the public mirror.

- Semgrep runs the registry packs for the languages in the tree (Python,
  JavaScript, TypeScript, and Terraform), plus the secrets pack and the
  GitHub Actions pack. A finding at error severity fails the check. The
  result is kept as a build artifact. It is not uploaded as a
  code-scanning alert.
- pip-audit checks the declared Python dependencies. A high or critical
  finding fails the check. A finding below high is reported and does not
  fail it.

## Weekly check

Once a week, a check lists source issues with the `from-public` label that
are closed and have no public reply of the form above. It keeps a single
private tracking issue up to date. That list is not published to the
mirror. It exists so a close cannot drop the public conversation on the
floor.
