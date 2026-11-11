# Changelog

## [0.2.1] - 2026-11-11

### Fixed

- Guard Recorder attribute/event-data deletion with an atomic reference check,
  retaining foreign keys and native batching. The workaround accepts the exact
  reviewed HA 2026.11.0/2026.11.1 source and leaves other versions unchanged.
- Carry the same guard through native recovery without altering stored devices,
  configuration, statistics, or unrelated databases.

### Added

- Opt-in Recorder storage supervision in the existing Core updater: five-minute
  physical-size checks, one-day purge requests with durable admission, and a
  latched raw-recording pause at 4.5 GB. Core approval and purge admission share
  a lock; unknown purge outcomes cannot be replayed automatically.
- A schema-scoped DBA procedure for physical-size reporting without granting HA
  global PROCESS access, plus a narrow single-host MySQL egress rule.

### Upgrade

Install and verify the guarded runtime and read-only storage procedure before
qualifying automatic purge. The budget guard is disabled in the example settings;
operators opt in with their private schema/address. The 5 GB target is an operating
budget, not a hard quota: statistics and other database writes continue during a
raw-recording pause. Resuming a latched pause requires reviewed manual recovery.
See `docs/recorder-storage-budget.md` and `docs/recorder-orphan-guard.md`.

### Security

Database access reuses the existing private Recorder connection and HA API token.
Only the schema-scoped size procedure needs a DBA-defined execution context;
HA receives no global PROCESS privilege. Public examples contain no installation
identifiers. Guarded deletes preserve foreign keys, and a stale or unknown purge
receipt blocks Core approval until its outcome is reviewed.

## [0.2.0] - 2026-11-03

### Added

- Opt-in stable Core proposal workflow: prepare a pull request with exact runtime
  and backup image digests before an update is offered.
- After explicit approval, require fresh configuration backup and target
  preflight before requesting the exact PR merge through the existing SSH key.
  Verify merged source before deploying; retain legacy mode by default.
- Support an authenticated native HA update entity while keeping MQTT status
  read-only. Reject approvals during repository maintenance in the server too.

### Upgrade

Install the proposal workflow in the deployment repository first, then enable
`proposal_workflow` and migrate the HA entity before enabling
`native_update_entity`. See `docs/core-update-proposals.md`. No Core upgrade is
started by installation. No new GitHub token is stored in HA or the updater.

### Security

Approval remains authenticated, single-use and bound to the exact version and
proposal head. MQTT never authorizes an install. The Actions token stays in its
job, merge requests use the existing SSH key, and ordinary GitHub merge rules
remain effective. Failed or ambiguous merges stop before deployment; review
private diagnostics and reconcile state before reoffering an update.

## [0.1.21] - 2026-11-01

### Fixed

- Continue repository maintenance after Home Assistant migrates HACS to
  Marketplace. Discover both platforms, deduplicate update entities, and accept
  either platform in the guarded direct installer and selector.
- Preserve existing guard identities, backup and version checks, sequential
  installation and restart latches. Exercise the same runtime failure scenarios
  against both platforms, including mixed inventories and firmware exclusion.
- Use each reviewed backup profile's dependency versions in the release fuzz
  oracle. Cover every profile and wrapped profile indexes, and reject mixed
  source/dependency combinations without changing the production contract.

### Upgrade

Install the updated maintenance package while its workers are idle. Keep the
existing automation, script and helper IDs so the Core updater's exclusion
guards continue to work. After the 2026.11 migration, verify the Marketplace
config entry instead of restoring the removed HACS component; see
`docs/maintenance.md` for migration and restart guidance.

### Security

Marketplace warning acceptance remains a user action in its UI. Maintenance
does not grant acceptance, bypass backup verification or install Core or device
firmware. Both repository platforms retain the same guarded installation and
restart behavior.

## [0.1.20] - 2026-11-01

### Added

- Add an opt-in `backup_mode: configuration` for Core updates where recorder
  history is expendable. It preserves encrypted native configuration backups,
  exact Job receipts, destination confirmation and all update checks while
  skipping the MySQL export. Full backups remain the default and weekly full
  jobs are unchanged.
- Discard only recognized local generated history files under the explicit
  configuration policy; reject unknown files, links and unfinished staging.
  Neither the working database nor existing NAS archives are modified.
- Add explicit `--configuration-only` offline extraction with required identity
  and archive validation. Configuration archives cannot silently satisfy full
  database recovery or full-backup update policy.

### Upgrade

Install matching updater and native-backup source while idle before selecting
the optional policy. Read the history-loss and recovery limits in
`docs/maintenance.md`; never downgrade over a migrated recorder database.

### Security

Configuration archives still contain credentials and device keys: retain the
configured encryption and private NAS access. The optional policy reduces
history retention, not access checks. It preserves authenticated approval,
exact-image validation and destination confirmation, rejects unknown or linked
local artifacts, and never deletes data from the running recorder database.

## [0.1.19] - 2026-11-01

### Added

- Allow an administrator to offer one exact published beta through private
  `release_version` settings. Stable discovery remains the default; approval,
  immutable images, backup checks and preflight remain mandatory.
- Compare beta versions correctly and prevent downgrade offers after a beta
  installation or return to stable discovery.
- Add the reviewed Home Assistant 2026.11.0b2 native-backup source profile with
  securetar 2026.10.0 and py-synologydsm-api 2.11.1. Unreviewed betas and mixed
  source/dependency profiles remain rejected.

### Upgrade

Adopt the verified source through the locked dependency workflow. To select the
reviewed beta, set the private updater's `release_version` to `2026.11.0b2`,
redeploy its code and settings, then approve the exact offered candidate. Remove
the setting after installation to resume stable release discovery. Verify the
native backup contract and configuration preflight before restarting Core.

### Security

Explicit beta selection does not bypass version-bound approval, digest pinning,
backup integrity or configuration validation. Release metadata must match the
requested tag. Unreviewed backup source profiles and dependency combinations
remain unsupported; the updater never automatically downgrades an installed beta.

## [0.1.18] - 2026-11-01

### Fixed

- Preserve literal hexadecimal escapes in POSIX archive names and relative link
  targets, including escaped systemd unit names, without decoding them.
- Share member-path validation between capture and restore so unsupported names
  fail during inventory before component payloads are copied.
- Reject extraction of literal-backslash paths on non-POSIX platforms before
  creating the destination. Existing traversal, link and integrity checks remain
  unchanged; offline ownership preparation explicitly requires POSIX.

### Upgrade

Adopt the verified source through the locked dependency workflow. No profile
changes are required. Previously rejected archives can be checked with the
updated verifier, but verification alone does not promote incomplete snapshots.

### Security

Escape sequences remain literal filename bytes. Absolute paths, traversal,
unsupported backslashes and unsafe link chains retain their existing rejection
or skip behavior. Platform checks prevent native path interpretation from
changing the meaning of accepted names during extraction.

## [0.1.17] - 2026-11-01

### Added

- Add an opt-in bounded local mirror for validating closed SQLite snapshots.
  Online backup and source transaction release remain unchanged; full read-only
  validation and tar input use the same local bytes. Final publication stays on
  NAS, with existing source guards, deadlines and retry limits.
- Render separate disk scratch and explicit peak ephemeral-storage budgets.
  Local raw capture and SQLite mirroring cannot share a component; default
  storage and guarded physical fallback behavior are unchanged.

### Upgrade

Adopt the verified source through the locked dependency workflow. Enable
`sqlite_validation_mirror` only after reviewing local disk capacity, free-space
floor and the rendered volume/resource changes. No profile change is required
for existing behavior.

### Security

Local mirrors use private directories, bounded writes, disk identity checks and
verified payload digests. Output validation errors never select a source
fallback. Scratch directories assume trusted processes with the worker's UID
or elevated privileges; they are not an isolation boundary against those actors.

## [0.1.16] - 2026-11-01

### Changed

- Compare ordinary files against their successful capture at the final source
  scan, avoiding unnecessary whole-component retries for changes before copying.
  Changes during or after copying still fail the existing consistency checks.
- In staged capture, read recently modified regular-file families later while
  keeping every database base before its sidecars. Direct ordering is unchanged.
  Member inventory, directory/symlink and SQLite guards, deadlines, retry limits
  and verified NAS publication remain in place.

### Upgrade

Adopt the verified source through the locked dependency workflow. The ordinary
file contract now describes observed end-of-copy state rather than unchanged
initial inventory; it does not promise an atomic filesystem snapshot. Read the
capture-time file classification and scheduling limits in the recovery guide.
No profile changes or additional permissions are required.

### Security

Ordinary files remain protected by descriptor/path identity and full metadata
checks during copying and at the final scan. A replacement before safe open is
accepted only if the captured version remains stable. No required data is
excluded; source access, scratch limits and atomic publication are unchanged.

## [0.1.15] - 2026-10-31

### Added

- Add opt-in local raw-tar capture with a hard byte cap and free-space floor.
  Per-file and SQLite scratch, compression and final publication remain on NAS;
  existing source consistency checks, retry counts and deadlines are retained.
- Render a separate disk-backed emptyDir and explicit ephemeral-storage budgets
  only for configured recovery entries. The default storage path is unchanged.
- Add opt-in WAL read snapshots with a source-filesystem free-space floor to
  prevent repeated incremental-backup restarts under concurrent commits. Release
  each reader before destination validation; other journal modes are unchanged.

### Upgrade

Adopt the verified source release through the deployment repository's locked
dependency workflow, including the new local_capture.py module in both worker
source ConfigMaps. Review local disk capacity, private scratch permissions and
the rendered volume/resource changes before explicitly enabling local capture.
Measure capture duration before rollout. Existing running Jobs are unchanged.
Enable WAL snapshots separately only after reviewing source-disk headroom for
WAL growth while the read snapshot is held.

### Security

Local raw captures contain the same sensitive data as NAS backups. The worker
enforces owner-only scratch, rejects symlinks and source/destination overlap,
and fails closed on capacity or identity errors. A dedicated Kubernetes emptyDir
may be made private only after descriptor and mount verification. Space checks
do not reserve capacity against other writers; interrupted Pods may retain raw
data until deletion. No source exclusions or consistency checks are weakened.
WAL snapshots keep source access read-only and check its disk floor on every
backup progress callback. They can delay checkpoints; checks cannot reserve
space against writers or interrupt a blocked filesystem operation.

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
