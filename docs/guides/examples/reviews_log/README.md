# Reviews log

An example log for continuous review (#1817, ADR-274). A reviewer from outside the site holds a
read grant for one log until a stated date, reads it within that window, and signs a `REVIEW`
(meaning `reviewed`) naming the log and the range of entries reviewed. The site reads coverage of
the log in one query: how many reviews, the last review, and the ranges never reviewed.

## Install it

```bash
cp -r docs/guides/examples/reviews_log <project>/.axi/extensions/
axi log validate <project>/.axi/extensions/reviews_log/logbook/reviews_log.toml
```

## Give a reviewer access

```bash
axi log role grant @reviewer:site reviewer --log <reviewed log> --until 2027-03-31
axi grant list --kind read          # the grant, its expiry and who issued it
axi grant revoke <id>               # ends it; the next read is refused
```

The grant reads only the named log. It confers no role in that log or in this one beyond signing
a review of it, and an expired grant answers exactly as no grant does.

## Coverage

`GET /api/v1/attest/<reviewed log>/reviews/coverage`, or the skill `attest.reviews_coverage`,
returns the number of reviews, the last review (when, by whom), the entries reviewed and never
reviewed, and the same per month. A review counts only when the caller may read this log.
