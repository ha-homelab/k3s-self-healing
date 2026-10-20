# Changelog

## [0.1.9] - 2026-10-20

### Fixed

- Preserve the last verified recorder export when staging directories or
  metadata are linked to another generation. Publish metadata with private,
  exclusively created temporary files and atomic replacement.
- Validate all native backup resources before creating the export directory or
  applying changes. Refuse installation while an unfinished Job uses the shared
  code, and recheck the HA Pod and Job state immediately before applying.
- Select the HA container by its configuration mount, require the backup workers
  to match its immutable image digest, and verify the installed native backup
  contract before making changes.

### Upgrade

Align both HA worker images in the installation's native backup template with
the running, reviewed HA image before rerunning the installer. Schedule this
operation outside backup runs and HA rollouts. Publication alone changes no
running workload or recovery data.

### Security

Linked recorder staging or metadata now fails closed without rewriting the
previous verified pair. Installation preflight does not make multi-resource
application atomic; concurrent installers and backup starts remain unsupported.
Backup authorization, retention, runtime source checks and capacity reserves are
unchanged.

## [0.1.8] - 2026-10-19

### Fixed

- Budget native backup staging on its actual filesystem when Home Assistant uses
  a dedicated temporary-archive mount. Preserve independent free-space reserves
  for the recorder export and archive instead of charging both to the config disk.

### Upgrade

Re-run the native backup installer with the existing private node, database CIDR,
time zone and configuration PVC settings. It discovers Home Assistant's existing
temporary-backup hostPath and mounts it read-only into the backup workers for
capacity checks. Installations with in-PVC temporary archives retain the existing
single-filesystem budget. Publishing this source does not change running jobs.

### Security

The installer verifies the running HA node, configuration PVC/subPath and dedicated
mount. Workers reject a missing or changed staging device instead of silently
using the root filesystem. No backup retention or recovery validation is relaxed.

## [0.1.7] - 2026-10-19

### Fixed

- Record the selected Music Assistant data claim in snapshot metadata after a
  storage migration. Invalid claim names fail before creating backup files.

### Upgrade

Set `SOURCE_DATA_CLAIM` to the PVC mounted at `/data` in the Music Assistant backup
job. Existing jobs retain the `music-assistant-home-data` default. Publishing the
source archive does not modify running jobs or migrate storage.

### Security

The claim selector accepts Kubernetes DNS names and affects snapshot attribution
only. Preserve private snapshot permissions and verified restore procedures when
adopting the release; this change grants no additional filesystem or cluster access.

## [0.1.6] - 2026-10-18

### Changed

- Maintain reusable HA packages, the motion-light blueprint and runtime tests
  directly in this repository. Source validation and packaging no longer fetch
  or regenerate them from an external configuration repository.

- Document configuration ownership, private approval receipts, and the source validation and release process.

### Upgrade

Review deployment-specific configuration before manually adopting the verified source archive. Preserve private state, approval receipts and the previously accepted source package for rollback. Publishing the archive does not apply Kubernetes manifests or alter running services.

### Security

Approval, backup, source-push and recovery gates remain in force. Private runtime evidence stays outside the source repository; documentation and synthetic checks do not qualify a live deployment.

## [0.1.5] - 2026-10-17

### Changed

- Refresh maintained Home Assistant runtime references to 2026.10.2 and explicitly select PBKDF2-HMAC-SHA512 for new Mosquitto password files.
- Add whole-source security analysis and improve reproducible source-package diagnostics and documentation.

### Upgrade

Review deployment-specific configuration before manually adopting the source package. Configure final administrative endpoints directly because redirects are now rejected. Preserve private backup credentials and the previously accepted source package for rollback. Publishing this archive does not apply Kubernetes manifests or alter running services.

### Security

Administrative Home Assistant and Kubernetes HTTP/WebSocket clients reject redirects before forwarding credentials. Native-backup credential files are created exclusively with mode 0600 and removed after dump completion or failure. Existing empty restore directories become private before backup extraction. These changes include source and synthetic validation, not a claim of production deployment verification.

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
