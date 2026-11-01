# Changelog

## [0.1.15] - 2026-10-31

### Added

- Add opt-in local raw-tar capture with a hard byte cap and free-space floor.
  Per-file and SQLite scratch, compression and final publication remain on NAS;
  existing source consistency checks, retry counts and deadlines are retained.
- Render a separate disk-backed emptyDir and explicit ephemeral-storage budgets
  only for configured recovery entries. The default storage path is unchanged.

### Upgrade

Adopt the verified source release through the deployment repository's locked
dependency workflow, including the new local_capture.py module in both worker
source ConfigMaps. Review local disk capacity, private scratch permissions and
the rendered volume/resource changes before explicitly enabling local capture.
Measure capture duration before rollout. Existing running Jobs are unchanged.

### Security

Local raw captures contain the same sensitive data as NAS backups. The worker
enforces owner-only scratch, rejects symlinks and source/destination overlap,
and fails closed on capacity or identity errors. A dedicated Kubernetes emptyDir
may be made private only after descriptor and mount verification. Space checks
do not reserve capacity against other writers; interrupted Pods may retain raw
data until deletion. No source exclusions or consistency checks are weakened.

## [0.1.14] - 2026-10-31

### Changed

- Stage one ordinary file of up to 64 MiB in memory during opt-in staged
  capture, avoiding its extra NAS write/read cycle. Direct compression keeps
  the 8 MiB limit; larger files and SQLite snapshots continue using NAS scratch.
- Preserve source consistency checks, per-file retries, shared deadlines and
  archive verification. The payload limit does not bound total worker memory;
  review memory headroom before adopting the source update.

### Upgrade

Adopt the verified source release through the deployment repository's locked
dependency workflow. Review worker memory headroom and measure staged capture
before rollout. Existing running Jobs are unchanged; no profile change is needed.

### Security

The change grants no permissions and excludes no source data. Filesystem and
SQLite consistency checks, owner-only scratch and atomic publication remain
unchanged. The bounded payload allocation does not guarantee total memory use.

## [0.1.13] - 2026-10-31

### Added

- Add opt-in `staged_capture` for recovery components. Capture an uncompressed
  tar and verify the source before compression, so compression no longer
  requires the live source to remain unchanged. Record capture and compression
  durations for successful staged attempts.
- Keep direct compression as the default. Reject non-boolean settings before
  backup side effects; retain the existing component deadline and retry count.

### Upgrade

Adopt the verified source release through the deployment repository's locked
dependency workflow. Enable staging only for reviewed components with space for
the raw tar, compressed archive, concurrent file/database scratch and reserve.
The initial free-space check does not reserve capacity. Measure the capture
window; staging does not guarantee a stable source. Running Jobs are unchanged.

### Security

Source consistency guards, archive verification and atomic publication remain
unchanged. Staging grants no permissions and excludes no source data. Temporary
raw archives remain owner-only; abrupt termination can leave private scratch
that requires inspection before cleanup.

## [0.1.12] - 2026-10-31

### Fixed

- Allow a reviewed per-component archive time budget for larger recovery sources
  instead of requiring every source to fit the default 15 minutes. Reject invalid
  budgets before creating backup state or contacting the cluster.
- Keep the 15-minute default and bound overrides to one hour. All retries for a
  component share that budget; archive integrity checks and the overall Job
  deadline remain unchanged.

### Upgrade

Adopt the verified source release through the deployment repository's locked
dependency workflow. Set `archive_timeout_seconds` only on a measured claim or
host source in the protected recovery profile, and allow room for the remaining
components within the Job deadline. Do not remove application assets or rollback
data merely to make a backup finish faster. Existing profiles need no change.
Updating the source does not change active Jobs or running applications.

### Security

The option changes a bounded capture budget only. It grants no permissions and
does not change source selection, filesystem guards, consistency checks, capacity
reserves, archive format, retention or publication requirements.

## [0.1.11] - 2026-10-29

### Updated

- Pin the native recorder export's official MySQL client to 8.4.13 for ARM64
  and AMD64. This LTS patch includes `mysqldump` metadata-validation fixes
  ([upstream release notes](https://dev.mysql.com/doc/relnotes/mysql/8.4/en/news-8-4-13.html)).
- Verify architecture selection without changing the export pipeline, HA worker
  images, security settings, format, limits, or retained backups.

### Upgrade

Adopt the verified source release in the private deployment, preserve its HA
image pins and storage settings, and run the native backup installer only after
all existing backup Jobs have finished. Running Jobs keep their original image.
This updates the client used by future exports; it does not upgrade the database
server or rewrite existing exports. Qualify the exact image and available disk
space before installation. Keep the previous accepted source for rollback.

### Security

The official client update includes upstream validation fixes for server-supplied
metadata. Per-architecture digests remain immutable, and the read-only container,
private credential handoff, export size limit, and previous-export retention are
unchanged. Qualify the new client before allowing future backup Jobs to use it.

## [0.1.10] - 2026-10-29

### Fixed

- Omit only reviewed Home Studio generated assets and installer/maintenance rollback directories
  from the Core schema-check copy. Preserve Store files, integration code,
  configuration includes, and existing symlink confinement.
- Measure the selected configuration before copying. Reject the existing byte
  cap early and allow up to five minutes for sufficient temporary disk capacity,
  retaining the native-backup reserve and checking space during copying.

### Upgrade

Adopt this source release through the private deployment's verified dependency
workflow before installing the updated Core preflight code. These exclusions
apply only to schema validation, never full startup or backup. Publishing the
source does not update a running worker or change backup retention.

### Security

Configuration-copy, report-validation, approval and release gates remain in
place. The capacity wait only observes free space; it does not delete data or
relax the reserve. It cannot prevent unrelated processes from consuming disk.

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
- Keep Core updater runtime endpoints aligned with the installer's selected
  namespace and HA/MQTT Services. Reject conflicting settings before applying
  manifests and remove unused endpoint environment variables.
- Reject custom StatefulSet names that the fixed-name updater worker cannot
  support.

### Upgrade

Align both HA worker images in the installation's native backup template with
the running, reviewed HA image before rerunning the installer. Schedule this
operation outside backup runs and HA rollouts. Publication alone changes no
running workload or recovery data.

Re-run the Core updater installer with the existing private settings and service
names. Omit `ha_url` and `mqtt_host` to derive them from the selected Services, or
set them to the same in-cluster endpoints. The installer still defaults to a
server-side dry run. Publishing this source does not change a running updater.

### Security

Linked recorder staging or metadata now fails closed without rewriting the
previous verified pair. Installation preflight does not make multi-resource
application atomic; concurrent installers and backup starts remain unsupported.
Backup authorization, retention, runtime source checks and capacity reserves are
unchanged.

The runtime settings and NetworkPolicy now describe the same administrative
endpoints. Conflicting values fail before manifest application; no credential,
approval, backup or release-acceptance gate is relaxed.

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
