---
name: data.edge_bundle
description: Write the ingest edge's deploy folder (Dockerfile, compose file, nginx blocks, operator script, README) from the installed package, pinned to this release, so the person deploying needs no file transfer.
---

# data.edge_bundle

`axi data edge-bundle [--out DIR] [--tar] [--version X.Y.Z] [--force]`

Writes the folder an ingest edge (ADR-177) is deployed from, with the container's
`axiom-os-lm` pin and image tag set to the release that wrote it. A build that is
not a published release (a local or development install) refuses to pin itself;
pass `--version` to name the release the container should install.

`--tar` also writes `ingest-edge-<version>.tar.gz` next to the folder and prints its
sha256. The archive is reproducible: the same release gives the same bytes.

It never overwrites a folder it did not make, unless `--force`.
