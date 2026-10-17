# Installation

## Requirements

- A working k3s cluster and explicitly selected kubectl context.
- Python 3.14, the pinned validation dependencies, and Helm 3.
- A `local-path` StorageClass, or your own replacement in the site profile.
- A dedicated HA node with persistent storage; Pod recreation preserves its PVC,
  but node loss needs recovery rather than scheduling onto an empty disk.

All example addresses are documentation addresses. Models such as Raspberry Pi,
Synology NAS and iPhone may be named publicly; serials, MACs and recipient details
must be supplied privately.

## Local profile

```sh
mkdir -p local
cp examples/site.example.json local/site.json
```

Edit the profile before applying anything. `node` hosts HA and Mosquitto;
`lan_node` is used by the optional Music Assistant recipe. `storage_class` is
the application PVC class. `nas_ipv4`, `nas_export` and
`recovery_storage_class` configure the optional NAS backup path.
`kubernetes_service_ipv4` and `node_ipv4` set the optional recovery API policy.
`deployment_repository` is a GitHub SSH URL for the updater's private desired
state repository. All profile fields are required so implicit installation
defaults cannot silently select the author's infrastructure.

Keep real profiles outside published files. The renderer preserves them only in
local output. It does not connect to a cluster or apply resources.

## Mosquitto

Generate a password file with Mosquitto 2.1.2 `mosquitto_passwd`; let it prompt for the password.
Select PBKDF2-HMAC-SHA512 explicitly with 220,000 iterations, as in the
[OWASP password-storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html#pbkdf2).
This avoids depending on the hashing default of a different installed tool version.
Do not use a command-line password, commit the file, or publish its hash.

```sh
umask 077
mosquitto_passwd -H sha512-pbkdf2 -I 220000 -c local/mosquitto-password.txt homeassistant
kubectl --context YOUR_CONTEXT create namespace homeassistant
kubectl --context YOUR_CONTEXT -n homeassistant create secret generic mosquitto-passwd \
  --from-file=password.txt=local/mosquitto-password.txt
python tools/render.py --profile local/site.json companions > local/companions.yaml
kubectl --context YOUR_CONTEXT apply --dry-run=server -f local/companions.yaml
kubectl --context YOUR_CONTEXT apply -f local/companions.yaml
```

For an existing password file, omit `-c` and run the same hashing options for
each user to reset its password without overwriting the other users. Existing
hashes are not upgraded automatically. Keep the file readable by the broker
through its Secret mount.

This broker has no device bridges or external listener. In HA, configure MQTT
with `mosquitto.homeassistant.svc.cluster.local`, port 1883, and your credentials.

## Home Assistant

```sh
python tools/render.py --profile local/site.json helm > local/ha-values.yaml
helm upgrade --install home-assistant \
  https://github.com/pajikos/home-assistant-helm-chart/releases/download/home-assistant-0.3.80/home-assistant-0.3.80.tgz \
  --kube-context YOUR_CONTEXT --namespace homeassistant \
  --values local/ha-values.yaml --wait --timeout 15m
kubectl --context YOUR_CONTEXT -n homeassistant port-forward svc/home-assistant 8123:8123
```

Open `http://localhost:8123` and create your own administrator. Supply your
location and timezone during onboarding; the source has no household location.

Before enabling an updater, put the *rendered* Helm values at
`helm/values-pajikos.yaml` and your reviewed native backup manifest at
`native_backup/cronjob.yaml` in your private deployment repository. The worker
updates these exact paths on its `main` branch. It does not update this shared
public source repository.

## Optional companions and guards

ESPHome, Ring-MQTT and Music Assistant have separate files under `companions/`.
Render a selected recipe with `python tools/render.py --profile local/site.json
esphome` (or `ring-mqtt`, `music-assistant`) into a local file. Review it, provide
its required Secrets, then apply to the explicit context. Ring-MQTT expects `ring-mqtt-env`;
ESPHome stores firmware configuration and its local secrets on its PVC.
Music Assistant uses host networking for LAN discovery and must be placed on a
node on the intended LAN. Do not expose its service without authentication.

`helm/go2rtc-startup-guard` and `helm/smartthings-time-guard` contain optional
version-sensitive compatibility patches. The base chart does not enable them.
Their tests establish the supported transformations; recheck them against a new
HA version before privately mounting them under `/etc/cont-init.d/`.
