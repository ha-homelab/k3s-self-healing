# Maintenance

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
06:00 schedule is an example in HA's configured timezone. Change it locally.
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
release names require additional worker changes, not only installer flags.

An approval never means "install whichever version is newest later". A failed
backup, wrong receipt, interrupted write, or unsuccessful preflight stops the
operation. The durable journal distinguishes an ambiguous result from a proven
failure. Do not clear it merely to force a retry. After a database migration,
restore from the verified matching backup rather than downgrade the image over
the migrated database.
