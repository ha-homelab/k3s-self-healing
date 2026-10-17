# Public source policy

Public: reusable implementation, tests with synthetic data, generic schemas,
device models, upstream links, dependency pins, licenses, and GitHub authorship.

Private: phone numbers, personal email and notification recipients, home location,
device MAC/serial/account identifiers, actual installation endpoints and cloud
resource IDs, passwords/tokens/keys, live configuration, pairing/authentication
storage, databases, backups, deployment receipts and operational logs.

This repository was initialized independently from selected reviewed files.
Private Git histories, branches, tags, PR discussions and runtime state are not
part of this repository. Contributions to its reusable implementation belong
here. Do not merge a private predecessor's Git history or mirror-push it.

## Shared source ownership

This repository owns the portable update controller, backup/recovery tools,
deployment recipes, HA packages, motion-light blueprint and runtime tests.
Maintain reusable logic directly in these checked-in files, including the Core
approval and HACS maintenance packages and their tests. Source validation and
packaging use this repository alone; there is no external HA configuration
checkout, source-regeneration step or source-import lock.

Operators consume an exact reviewed revision and keep installation configuration
in a private repository. Use private overlays for addresses, credentials,
notification recipients and installation-specific schedules. Propose reusable
behavior changes here with synthetic regression tests.

```sh
python tools/validate.py --lint
python -m unittest discover -s tests
```

Review the diff and run the pinned HA runtime tests when changing a package or
its runtime tests. Keep approval endpoints and notification recipients behind
the existing secrets and private configuration boundaries. Never copy a live HA
configuration into this repository or a source archive.

Home Assistant container images, Python dependencies and the Helm chart remain
explicitly versioned dependencies. Their updates require separate review and
tests; editing a local HA package does not update those runtime dependencies.

`publication.json` explicitly lists every approved source file. `tools/validate.py`
rejects inventory drift, symlinks, unreviewed binaries and common identifiers.
`tools/package_release.py` checks a clean commit and writes a deterministic source
archive with normalized ownership and timestamps. It never adds ignored local
profiles, Git metadata, container caches or files discovered outside the list.

Before each release, run the privacy checker, a secret scanner, the test suites
and a manual review of identifiers and context. Inspect the final archive, not
only the source directory. If adding container publishing later, inspect layers,
history, labels, SBOM/provenance, build logs and signatures as well. This initial
release publishes source only and references upstream container images.

The pattern scanner is a guardrail, not proof of anonymity: prose, encoded values,
binary metadata and already public copies require contextual review. Keep any
private identifier denylist and source-to-public revision mapping outside this
repository. A published credential must be revoked, not just deleted in a later
commit. Review old public history separately when removing previously published
personal data.

The source CI and local review command include `--lint`, which requires the pinned
Ruff dependency. Archive packaging independently repeats inventory, syntax
and privacy checks; it does not require a linter in the minimal
release-contract environment. Required source CI still rejects every diagnostic.
