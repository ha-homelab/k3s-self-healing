# Maintenance

For the illustrated workflow and evidence limits, see the [visual guide](process-and-evidence.md).

## HA packages

Copy the selected files from `homeassistant/packages/` into your HA packages
directory and enable `homeassistant.packages: !include_dir_named packages` in
your private configuration. Merge into an existing `homeassistant` section.
Populate the three entries in `homeassistant/secrets.yaml.example` locally.
For optional notifications, also copy `homeassistant/python_scripts/` into your
HA configuration directory and enable `python_script:` in `configuration.yaml`.
The helper checks the current service registry on every delivery, so a provider
that loads late or is removed does not leave a stale availability result.

`core_updates.yaml` exposes exact candidate/version approval over authenticated
HTTP; MQTT conveys status only. `optional_notifications.yaml` skips an absent
notification provider. Leaving `optional_phone_notify_service` as
`notify.not_configured` disables the mobile channel; persistent HA notifications
remain available and `script.core_update_approve` can be called from HA's UI.
The secret value for a configured phone is its HA notify service, not a telephone
number. Do not copy another installation's mobile entity or account identifier.

`hacs_maintenance.yaml` performs sequential HACS or Marketplace updates only after verifying a recent
backup, and defers restart until scripts and automations are idle. Its Sunday
06:00 schedule is an example in HA's configured timezone. Put an installation's
schedule change in its private deployment overlay. Maintain reusable package
logic and its runtime tests directly here, following the
[shared source ownership policy](publication.md#shared-source-ownership).
It pauses around active Core updates. It does not upgrade Core or device firmware.

Home Assistant 2026.11 migrates HACS repository entities and devices into the
built-in Marketplace integration and removes the old HACS code and stores.
Check the migrated `marketplace` config entry before treating a missing
`custom_components/hacs` directory as data loss; do not restore the old component
over a completed migration. The package accepts and deduplicates update entities
from both platforms, including in the direct installer selector and guard.
Its automation, script and helper IDs stay unchanged for the Core updater.

Marketplace keeps the device model `integration`, so successful integration
updates still set the persistent restart latch. Plugin and theme updates do not.
The legacy HACS restart-summary check remains; Marketplace does not provide that
summary, so follow its own notices for updates installed outside this package.
Warning acceptance belongs in Marketplace's UI: a missing acceptance stops
maintenance without bypassing the warning. Backup confirmation, installation
verification, timeouts, Core exclusion and the final idle fence apply to both
platforms. See the upstream [migration](https://github.com/home-assistant/core/blob/2026.11.0b2/homeassistant/components/marketplace/migration.py),
[update](https://github.com/home-assistant/core/blob/2026.11.0b2/homeassistant/components/marketplace/update.py)
and [device](https://github.com/home-assistant/core/blob/2026.11.0b2/homeassistant/components/marketplace/entity.py) contracts.

Import `homeassistant/blueprints/motion_light_off.yaml` through HA's blueprint
mechanism or copy it into your blueprint directory. Choose your own sensors,
lights, inactivity durations and manual override conditions. It retries expired
shutdowns when devices return without treating an ordinary manual ON as recovery.

## Core updater prerequisites

The worker supports an existing `home-assistant` Helm release on chart 0.3.80,
the `home-assistant` StatefulSet, and a `ha-recorder-native-backup` CronJob in the
same namespace. It needs the matching native backup API Secret and a working
Synology DSM native backup destination. The default full-backup policy also
requires a working MySQL recorder. Verify these first.
The approved contract pins inspected HA image/runtime versions; unknown versions
must pass contract review before the backup path will accept them.

Create a private desired-state repository containing the deployment paths
described in `installation.md`. Select it explicitly in the site profile. The
worker rejects example, local-path and non-GitHub-SSH remotes, verifies the actual
checkout origin, refuses dirty state, and fetches only `main`. Give its SSH key
write permission only to that private repository and pin GitHub's verified host
keys in `known_hosts`.

Create a mode-0600 JSON file containing exactly `approval_token`, `mqtt_username`,
`mqtt_password`, `ssh-privatekey`, and `known_hosts`. Generate your own approval
token with at least 32 characters. The installer transmits credentials on stdin
and does not print their values.

```sh
python tools/render.py --profile local/site.json updater-settings > local/updater-settings.json
python scripts/install-core-updater.py --kubeconfig /private/path/kubeconfig \
  --settings-file local/updater-settings.json --credentials-file local/updater-credentials.json
```

The first command only renders; the installer defaults to server-side dry run.
Repeat the installer with `--apply` after reviewing the discovered PVC/node.
Its default names are the supported release layout above. Nonstandard Helm
release and StatefulSet names require additional worker changes; the installer
rejects unsupported StatefulSet names.

For a different namespace or HA/MQTT service name, select it with `--namespace`,
`--ha-service` and `--mqtt-service`. The installer writes the matching `ha_url`
and `mqtt_host` into `settings.json`, which is the updater's endpoint source.
Omit those settings to let the installer fill them, or supply exactly matching
values; stale or conflicting values fail before any manifest apply. The unused
`HA_URL` and `MQTT_HOST` environment variables no longer appear in the manifest.

Release discovery uses the latest stable version by default. An administrator
can set `release_version` in the private updater settings to an exact published
stable or beta version, such as `2026.11.0b2`. This selects an offer; it does not
approve installation. The authenticated candidate approval, immutable image,
native backup and configuration checks remain required. Drafts, development
tags, mutable channel names and mismatched release metadata are rejected.
The version ordering puts every beta before its final stable release and never
offers a downgrade. Remove `release_version` after the deliberate transition to
resume stable discovery; an installed beta newer than stable stays installed.

The `2026.11.0b2` native-backup profile was reviewed against the complete upstream
backup module inventory and Synology uploader. Its store migration adds the last
backup size; the create actions and streamed archive format remain compatible.
Upload streams now close after success or failure, and Synology reports progress.
The profile pins securetar `2026.10.0` and py-synologydsm-api `2.11.1` together with
the source hashes. Dependency versions cannot be mixed with the older profile.
Other beta versions need their own exact review even if their files are identical.
Source eligibility does not replace a successful target-image preflight or
verification that the configured NAS accepted a real backup.

An approval never means "install whichever version is newest later". A failed
backup, wrong receipt, interrupted write, or unsuccessful preflight stops the
operation. The durable journal distinguishes an ambiguous result from a proven
failure. Do not clear it merely to force a retry. After a database migration,
restore from the verified matching backup rather than downgrade the image over
the migrated database.

### Optional configuration-only update backups

Set `"backup_mode": "configuration"` in private updater settings only when
recorder history is explicitly expendable. The default `"full"` policy is
unchanged. Unknown values fail; there is no automatic fallback from a failed
full backup to a smaller one. Deploy matching native-backup and updater source
while both are idle before selecting this policy.

For an approved Core update, the configuration policy removes the MySQL export
init containers from that one Job. It uses HA's native `backup/generate` action
with `include_database: false` and `include_homeassistant: true`, preserving the
configured destinations, encryption password and folder choices. Configuration,
credentials, registries, device keys, integrations and automations remain in
the archive. The running Oracle/MySQL database is neither read nor modified.

Old generated SQL files under `/config/recorder_backup` would otherwise bloat
every archive. This policy explicitly discards that local export cache after
checking HA is idle and every entry is a recognized regular, unlinked artifact.
Unknown files, links and unfinished export staging stop the operation. It does
not delete existing NAS archives. Weekly full-backup CronJobs are unchanged.

The completed receipt records `backup_mode: configuration`, no recorder payload,
the exact update and Job identities, and confirmed destinations. A worker using
the full policy refuses that receipt. All image, approval, preflight and health
checks remain mandatory. These are manually requested native archives; inspect
their retention separately from HA's automatically scheduled backups.

This choice gives up matching recorder history for rollback. Do not downgrade
Core against a database already migrated by a newer version. Restore config at
the same/newer compatible version, or deliberately provision an empty recorder
schema and accept lost history. The offline extractor requires explicit
`--configuration-only`; it still checks required identity files, archive paths
and compressed-stream integrity. It never starts HA or changes a database.

### Disposable Core configuration check

The preflight Job runs Home Assistant's schema-only `check_config` against a
private disposable copy. It keeps configuration YAML, secrets, includes, entity
and integration registries, and all custom-component code and packaged assets.
It does not start the complete Home Studio application.

Only these reviewed runtime **directories** are omitted from that copy:
`.storage/home_floorplan_generated_assets`,
`.storage/home_floorplan_studio_assets`, `.storage/home_floorplan/studio`,
`.home-studio-backups`, and root directories beginning `.home-studio-install-`.
These contain generated visualization data or installer rollback/staging copies,
not schema inputs. The same root-only rule omits directories beginning
`.asterisk-telephony-backup-`, `.epg-channel-backup-`, `.epg-media-backup-`,
`.ha-stale-retirement-`, or `.recorder-quality-backup-`. These are installer or
maintenance rollback copies, with duplicate configuration and private operation
receipts; they are not the active integration trees. This is an explicit list,
not a generic hidden-directory or backup-name filter.

Regular Store files at matching names are still copied;
symlinks still undergo the normal confinement checks. Similarly named adjacent
paths and the complete `custom_components/home_floorplan` tree remain included.
Do not reuse this omission policy for a full startup check or a backup.

Before creating the disposable directory, preflight measures the same selected
files and rejects configurations above the existing 3 GiB logical byte cap. It
allows for block-rounded file data, entry metadata and 64 MiB of temporary work,
while retaining the native-backup reserve: at least 3 GiB and 1 GiB above the
assumed 5% node filesystem eviction threshold. If that space is unavailable, it
waits up to 300 seconds using read-only checks, then stops with required and
available byte counts. It neither removes files nor lowers the reserve. Capacity
is checked again during copying, and growth still obeys the byte cap. This limits
copy-induced exhaustion; it cannot reserve disk against other processes. Image
pulls happen before this check and need their own operator capacity review. The
64 MiB allowance is not a hard limit on later validation subprocess writes.

### Private approval receipts

Before changing either source manifest, the updater saves the six-field approval
receipt under `/state/approvals/<candidate-id>.json` on its existing state PVC.
This contains the backup identifier and approval time. The directory is private
(`0700`), files are `0600`, and a complete receipt is published without replacing
an existing candidate record. An inaccessible or non-private directory stops the
operation before source files change. A pre-existing receipt requires operator
inspection; it is not silently reused or overwritten.

Git commits contain only the approved Helm and native-backup image pins. Keep
`/state/approvals` and the durable updater state in private backups. Receipts
document approval and backup selection, not successful deployment; use the
durable operation state to resolve an interrupted commit, push, or Helm operation.
When upgrading an older deployment, preserve its previous Git-based receipts in
private operator storage. This change does not remove historical Git objects or
alter a running deployment until its updater code is explicitly installed.
