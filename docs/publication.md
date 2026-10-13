# Public source policy

Public: reusable implementation, tests with synthetic data, generic schemas,
device models, upstream links, dependency pins, licenses, and GitHub authorship.

Private: phone numbers, personal email and notification recipients, home location,
device MAC/serial/account identifiers, actual installation endpoints and cloud
resource IDs, passwords/tokens/keys, live configuration, pairing/authentication
storage, databases, backups, deployment receipts and operational logs.

This repository was initialized independently from selected reviewed files.
Private Git histories, branches, tags, PR discussions and runtime state are not
part of this repository. New contributions belong here; do not merge a private
predecessor's Git history or mirror-push it.

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
