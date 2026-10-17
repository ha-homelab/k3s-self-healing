# Changelog

## Unreleased

### Security

- Reject all redirects while opening administrative Home Assistant and Kubernetes
  WebSockets. Previously, a redirected HA WebSocket could receive the configured
  access token; a Kubernetes redirect could also change the checkpoint endpoint.
  Configure the final endpoint directly. No CVE has been assigned.

- Make an existing empty native-restore directory private before opening a backup, preventing restored credentials from becoming visible through inherited shared-directory permissions.

- Reject redirects in administrative Kubernetes and Home Assistant HTTP clients before a token can reach a different endpoint; close private error responses promptly. No CVE is assigned.
- Add whole-source Python/Actions CodeQL analysis and document the unqualified network-transport boundary honestly. Enable private vulnerability reporting and publish contribution, conduct and continuity policies.

## [0.1.4]

### Fixed

- Keep Core update approval receipts, including backup identifiers and approval
  times, on the updater's private state volume instead of committing them to Git.
- Require private receipt permissions and publish complete receipts without
  replacing existing records before changing the two approved image-pin files.

### Upgrade

Back up the updater state volume and preserve older receipts in private storage.
An existing candidate receipt requires operator inspection after an interrupted
operation. Approval, backup, source-push and recovery gates remain in force.
Installing this source update and any live Core upgrade remain separate actions.

### Security

New approval receipts are created with owner-only access outside the source
checkout. Existing committed receipts need separate history cleanup; this update
does not remove historical copies or rotate any credentials.

## [0.1.3] - 2026-10-14

### Changed

- Validate each public release candidate with bounded, network-isolated fuzz campaigns against its exact source revision.
- Require positive engine execution counts, instrumented production code and validated seed corpora; retain source-bound reports and diagnostic evidence.
- Run deterministic seed and configuration checks in ordinary CI without starting mutation campaigns on betas.

### Upgrade

Use the existing installation procedure, preserve local configuration and keep the previous accepted package for rollback. This maintenance release updates release validation and does not change the application configuration format. Deployment and applicable device or integration acceptance remain separate from publication.

### Security

RC publication rejects absent or invalid source-bound fuzz reports. The fuzz runner bounds corpus paths, file types and size. No application vulnerability or new target-device acceptance is claimed by these tooling changes.

## [0.1.2]

### Added

- Add frozen beta/RC source plans and archive receipts while preserving the
  deterministic public-source distribution and companion SHA-256 file.
- Require Linux mutation campaigns for recorder metadata and runtime contracts
  before RC publication, alongside all existing source, privacy and HA tests.

### Upgrade

extract the exact source archive separately and review private site
  configuration before installation. No cluster, restore or update command is
  executed by packaging or release publication.

### Security

retain the explicit publication inventory, imported HA source
  integrity and privacy checks. The reserved example.test email domain is
  recognized as synthetic test data; actual identifiers remain rejected.
