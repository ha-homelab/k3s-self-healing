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

`hacs_maintenance.yaml` performs sequential updates only after verifying a recent
backup, and defers restart until scripts and automations are idle. Its Sunday
06:00 schedule is an example in HA's configured timezone. Put an installation's
schedule change in its private deployment overlay. Maintain reusable package
logic and its runtime tests directly here, following the
[shared source ownership policy](publication.md#shared-source-ownership).
It pauses around active Core updates. It does not upgrade Core or device firmware.

Import `homeassistant/blueprints/motion_light_off.yaml` through HA's blueprint
mechanism or copy it into your blueprint directory. Choose your own sensors,
lights, inactivity durations and manual override conditions. It retries expired
shutdowns when devices return without treating an ordinary manual ON as recovery.

## Core updater prerequisites

The worker supports an existing `home-assistant` Helm release on chart 0.3.80,
the `home-assistant` StatefulSet, and a `ha-recorder-native-backup` CronJob in the
same namespace. It needs the matching native backup API Secret and a working
MySQL recorder + Synology DSM native backup destination. Verify these first.
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

An approval never means "install whichever version is newest later". A failed
backup, wrong receipt, interrupted write, or unsuccessful preflight stops the
operation. The durable journal distinguishes an ambiguous result from a proven
failure. Do not clear it merely to force a retry. After a database migration,
restore from the verified matching backup rather than downgrade the image over
the migrated database.

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
