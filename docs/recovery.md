# Backup and recovery

## Recovery sets

The NAS recovery worker verifies a dedicated bound PVC/PV with `Retain`, checks
the NAS server and export path, and refuses the export root as its destination.
It verifies that mounted host state matches the selected Kubernetes node.
Archives include hashes and completion markers; incomplete runs never become a
successful recovery set. SQLite databases are copied through consistent backup
handling; Mosquitto persistence is explicitly checkpointed before capture.

Create an NFS StorageClass or statically bound PV/PVC for
`ha-disaster-recovery/recovery-snapshots`. It must have a dedicated directory below
`nas_export`, writable from the selected node, with `Retain` reclaim policy.
The rendered worker additionally mounts that export read-only to resolve NFS
application volumes. It reads local-path volumes and k3s server state on the HA
node. The initial profile covers the HA and Mosquitto PVCs. Select additional
installed PVCs explicitly through `recovery_components`; their absence fails
the backup rather than producing an incomplete success.

```sh
python scripts/deploy-disaster-recovery.py --context YOUR_CONTEXT \
  --profile local/site.json > local/recovery-preview.yaml
python scripts/deploy-disaster-recovery.py --context YOUR_CONTEXT \
  --profile local/site.json --apply
```

The apply path suspends the schedule until the PVC identity, NFS destination and
retention checks pass. A failed validation leaves it suspended. `--run-now`
requires `--apply`. Music Assistant backups are separately optional and require
the Music Assistant data and backup PVCs on the chosen LAN node.
Its worker requires `NODE_NAME`; the manifest supplies the actual scheduled node
through the Kubernetes downward API so recovery metadata never invents a node.

Recovery data contains HA authentication/pairing state, Kubernetes Secrets and
k3s server credentials. Restrict and protect the NAS, and keep recovery sets,
restore reports and approval receipts private. The source publication tooling
does not sanitize a real backup and must never be used to publish one.

## Native backup with MySQL recorder

This optional path exports a transaction-consistent external MySQL recorder,
checks the compressed stream and available capacity, then asks HA to create and
verify a native backup on Synology DSM. It requires the exact supported runtime
contract in `native_backup/contract.py`, a configured external recorder, and HA's
Synology backup destination. It deliberately refuses unknown streaming contracts.

```sh
python scripts/install-native-backup.py --kubeconfig /private/path/kubeconfig \
  --node YOUR_HA_NODE --database-cidr YOUR_DATABASE_IP/32 \
  --token-file local/ha-backup-token --timezone Etc/UTC
```

This installer **applies** the native backup resources. It creates the private
export directory and installs the weekly CronJob, not an immediate backup. The
token file must be mode 0600 or 0400. Confirm the first backup and restore before
enabling Core updates. The API token has the same sensitivity as HA login access.

## Restore

Use `python -m disaster_recovery.restore --help` for recovery-set verification and
extraction options. Use `python native_backup/restore.py --help` for native HA
archives. Extract into an empty isolated destination. The tools reject traversal,
duplicate members, unsafe links, mismatched hashes and unconfirmed completion.
They do not start HA or import a database as a side effect of extraction.

`helm/native-recovery` starts with zero HA replicas and a separate restore
workspace. `scripts/restore-native-backup.py` requires explicit NAS host and
directory values and checks that the workspace is still stopped and owned by the
expected release before resuming a transfer. Database import is an explicit
separate operation; preserve the original backup before using `import_database.sh`.

For a lost k3s server, use its matching server datastore **and token** with the
compatible k3s version. Do not restore this onto the running server. The default
profile does not capture all host networking, SSH or OS configuration; preserve
your host provisioning and external service access privately. Radios, cloud
accounts, registries and the NAS remain external recovery dependencies.
