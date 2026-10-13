# Public source policy

Public: reusable implementation, tests with synthetic data, generic schemas,
device models, upstream links, dependency pins, licenses, and GitHub authorship.

Private: phone numbers, personal email and notification recipients, home location,
device MAC/serial/account identifiers, actual installation endpoints and cloud
resource IDs, passwords/tokens/keys, live configuration, pairing/authentication
storage, databases, backups, deployment receipts and operational logs.

This repository was initialized independently from selected reviewed files.
Private Git histories, branches, tags, PR discussions and runtime state are not
part of this repository. Contributions to code owned here belong in this
repository; imported HA files follow the ownership rules below. Do not merge a
private predecessor's Git history or mirror-push it.

## Shared source ownership

This repository owns the portable update controller, backup/recovery tools,
deployment recipes and optional-notification helper. Operators consume an exact
reviewed revision and keep installation configuration in a private repository.
Changing an operator's configuration must not create a separate editable copy
of the shared implementation.

The Core approval package, HACS maintenance package, motion-light blueprint and
their two imported runtime test files are generated from
[ha-homelab/home-assistant](https://github.com/ha-homelab/home-assistant).
`ha-sources.lock.json` records the exact source commit, five allowed source paths,
source SHA-256 hashes, transformation versions and resulting file hashes. Those
five checked-in files are a reproducible distribution snapshot, not separate
implementation owners. Edit their logic upstream, then explicitly update this
pin. The portable optional-notification package, helper and tests are maintained
here and are not included in that upstream import.

```sh
python tools/ha_sources.py check             # offline output integrity
python tools/ha_sources.py check --upstream  # fetch and reproduce the pinned source
python tools/ha_sources.py refresh --revision FULL_REVIEWED_COMMIT_SHA
python tools/validate.py
python -m unittest discover -s tests
```

The importer fetches only the five allowed raw HTTPS paths at a full commit SHA.
It refuses redirects, bounds each response and keeps upstream source bytes only
in memory. Deterministic transformations replace site-specific notification
recipients and the approval endpoint with secrets, remove site-specific header
comments, and adapt test fixtures and paths. They fail if the expected source
structure changes. Every generated output passes the privacy scanner before
any files are written. Review the diff and run the pinned HA runtime tests before
committing an updated lock. Never copy the full upstream configuration into this
repository or a source archive.

Ordinary validation rejects direct changes to generated files. CI also downloads
the exact source revision and regenerates all five files, so changing an output
hash alone cannot hide a divergent implementation. Dependency updates are
explicit reviewed changes; neither a moved tag nor a new upstream commit
silently changes an existing release.

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
