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

In the worker's runtime `config.json`, each `claims` or `host_sources` entry may
set `archive_timeout_seconds` to an integer from 1 through 3600; omission keeps
the 900-second default. All entries, including absent optional sources, are
validated before filesystem writes or Kubernetes access. Use a larger budget
for required data that takes longer to capture, after reviewing its size and
storage throughput. Up to three attempts for one component share this budget;
the complete recovery Job still has a 5400-second deadline. This is a runtime
configuration field, not a site-profile key. Service metadata archives and the
separate Music Assistant worker retain the 900-second component default.

An entry may also set `staged_capture: true` (a strict boolean, default `false`),
independently of its timeout. This first writes an uncompressed tar to the same
private NAS scratch, closes it and checks the live source with the existing
file/tree and SQLite guards. Only then is the captured tar compressed. An ordinary
file must remain unchanged during its successful copy and match that captured
version at the final scan. Changes before its copy, including an atomic regular
file replacement, may be accepted. Changes after copying still reject the
attempt; later changes after accepted capture cannot alter the captured bytes.
The same deadline includes capture, compression and verification.
Successful staged components record `capture_seconds` and `compression_seconds`
for the accepted attempt. This moves compression outside the source-stability
window but does not guarantee capture between recurring source writes. It does
not create a transaction across files or services.

Both capture modes compare ordinary files against their successful copy's full
identity and metadata, rather than against the initial inventory. Under the
existing filesystem-signature checks, these payloads coexist at the observed
end-of-copy boundary. Directory and symlink identities, member inventory and
SQLite guards remain unchanged; the final scan is not an atomic filesystem
snapshot. File contents are classified at capture: a file captured as SQLite
retains its original identity checks, while a former database replaced by an
ordinary file before copying is checked as an ordinary file.

Staged capture reads regular-file families from oldest to newest according to
the newest initial modification time in each family, with deterministic path
ties. A base and its existing `-wal`, `-shm`, and `-journal` descendants stay
together, base first, even for ordinary files. Direct capture retains lexical
ordering. This heuristic can shorten the interval between copying a frequently
updated file and checking it, but preserved timestamps or concurrent writes can
still cause retries. It excludes no files and changes no retry or timeout limit.

Staged capture holds one ordinary file of up to 64 MiB in bounded memory to
avoid an extra NAS write/read cycle. Direct compression retains the 8 MiB
limit. Larger files and SQLite snapshots still use NAS scratch. These limits
bound the file payload, not total process memory; allow additional memory for
the interpreter, inventories, buffers and databases when sizing the worker.

Budget additional NAS space before enabling this mode. With uncompressed tar
size T, compressed size G and concurrent per-file/SQLite scratch S, peak attempt
scratch is approximately `T + max(G, S)`, plus filesystem/database overhead and
other retained data. Without a measured compression bound, allow roughly twice
the uncompressed payload plus that additional scratch and reserve. NAS traffic
also gains one full tar write and read. `minimum_free_bytes` checks free space
at run start only; it does not reserve capacity or continuously guarantee the
floor. Normal exceptions clean the attempt's temporary tar/gzip files. SIGKILL,
node loss or a lost mount can strand scratch under an incomplete snapshot;
confirm no worker uses it before removing it through the recovery procedure.

For a reviewed component whose raw capture is limited by NAS writes, add a
`local_capture` object alongside `staged_capture: true` in its runtime entry:

```json
{
  "staged_capture": true,
  "local_capture": {
    "directory": "/capture",
    "max_bytes": 3221225472,
    "minimum_free_bytes": 5368709120
  }
}
```

Omission keeps the existing NAS capture path. The object requires exactly these
three fields: a canonical absolute directory and positive integer byte limits.
It requires staged capture and is independent of the component timeout. Only the
raw tar moves to this directory; per-file/SQLite scratch, gzip, verification and
the final exclusive hard-link publication stay on the NAS. Source checks and
retry/deadline limits are unchanged. This reduces NAS traffic during capture but
does not guarantee a stable source or a particular capture duration.

The renderer adds a separate disk-backed `emptyDir`, never a memory volume, to
the recovery worker when an entry opts in. Raw-capture entries in one Job must share one
directory. Without a SQLite validation mirror, the largest configured raw cap becomes its ephemeral-storage request;
the cap plus 1 GiB becomes both the volume size limit and ephemeral-storage limit.
The extra allowance also covers worker logs and writable-layer usage; review it
against actual node capacity. No volume or resource changes are made without
the option, and the separate Music Assistant worker is unchanged. Render and
review the manifest before rollout; verify that the node's kubelet storage is
on the intended local disk. The example requests 3 GiB and limits usage to 4 GiB.

For direct use outside Kubernetes, create a dedicated directory owned by the
worker with mode 0700 under trusted ancestors. Missing roots, symlink ancestors,
and overlap with either source or destination (including bind aliases) fail
closed. Linux requires readable, valid mount identities; known memory and network
filesystems are rejected even for an already private directory. The worker only
changes an existing root's permissions when its held
descriptor and Linux mount information prove it is the dedicated disk-backed
Kubernetes emptyDir mount. It never changes an arbitrary ancestor's permissions.
Attempt subdirectories are 0700 and raw tar files are 0600.

Allow space for the complete uncompressed tar, including headers and padding.
Every raw write is capped; the worker checks available filesystem space before
capture for `max_bytes + minimum_free_bytes`, and before and after bounded writes
for the configured free-space floor. These checks use space available to the
worker; they are not a reservation against other writers, nor a guarantee from
`emptyDir.sizeLimit`. With this option, peak NAS attempt scratch is approximately
`max(G, S)` instead of `T + max(G, S)`; the local disk holds T separately. Include
filesystem overhead, retained backups and both disks' reserves in sizing.
Capacity, I/O or identity failures abort without falling back to NAS capture.
Normal exceptions remove both attempt scratch directories. SIGKILL or node loss
can leave local raw data until the Pod is deleted; container restart alone does
not remove its emptyDir. Keep recovery data private during cleanup.

Incremental SQLite backups can restart when another connection commits changes.
For a reviewed WAL database workload, its component entry may opt into
`"wal_snapshot": {"minimum_free_bytes": 5368709120}`. The object accepts exactly
one positive integer field; omission keeps the existing SQLite behavior. It is
independent of tar staging and local capture. Only a database confirmed to use
WAL receives an explicit read snapshot; other journal modes are unchanged.
The resulting copy represents that snapshot's revision while writer commits
continue. See SQLite's [backup API](https://sqlite.org/c3ref/backup_finish.html)
and [WAL concurrency](https://sqlite.org/wal.html#concurrency) documentation.

A pinned reader can delay checkpoints and grow the source WAL. Before enabling
this option, allow source-disk headroom for writes during the entire database
copy. The worker checks available space through the held source-directory
descriptor before pinning and on every backup progress callback, aborting below
the configured floor. The example preserves a 5 GiB floor on the **source**
filesystem, separate from NAS or local-capture reserves. This is a repeated
check, not a reservation or a bound on concurrent writers. Keep operational
disk monitoring; a blocked I/O call can delay the next check.

The read transaction is released immediately when database copying finishes
or raises, before destination integrity checks, compression or archive checks.
The existing shared deadline and guarded fallback policies remain in force;
this option never changes the source journal mode or checkpoints the source.
Successful components list actually pinned databases in
`sqlite_pinned_wal_snapshots`; that field is absent when none were pinned.

When NAS page reads make SQLite output validation slow, a component may explicitly
enable a bounded local validation mirror:

```json
{
  "sqlite_validation_mirror": {
    "directory": "/sqlite-mirror",
    "max_bytes": 4294967296,
    "minimum_free_bytes": 5368709120
  }
}
```

This option is independent of WAL pinning and tar staging, and is absent by
default. It accepts exactly a canonical absolute directory and two positive
integer byte limits. It cannot coexist with `local_capture` on the same
component. The online SQLite backup still writes to private NAS scratch and
releases any source read transaction immediately afterward. The worker makes
that output standalone, closes it, then copies it sequentially to local disk.
The complete existing `quick_check` runs read-only on the local file. Tar reads
that same validated file, with identity/stat checks and a copy-digest comparison;
the final gzip, CRC/layout/hash checks and exclusive publication remain on NAS.
No source journal mode, scope, retry count or deadline changes. The guarded
physical fallback paths retain their existing NAS validation; errors in mirror
copying, validation or private-output normalization do not select those fallbacks.
Actual mirrored databases are listed in `sqlite_validation_mirrors`.

The renderer adds a distinct disk-backed `sqlite-validation-scratch` emptyDir.
Each role has one configured directory and a volume limit of its largest cap
plus 1 GiB. Components run serially and clean their local files before the next
component. Because simultaneous local raw capture and a mirror are rejected,
the container ephemeral-storage request is the largest cap across both roles;
its limit adds 1 GiB. For raw cap 3 GiB and mirror cap 4 GiB in different
components, that means a 4 GiB request and 5 GiB limit. Review actual node
capacity and writable-layer/log overhead before applying the rendered change.

Mirror storage uses the same disk identity, trusted-directory, no-symlink,
source/destination overlap, hard byte cap and repeatedly checked free-floor
rules as local raw capture. Its task-owned 0700 directory assumes other
processes with the worker's UID or elevated privileges are trusted; the checks
do not claim protection against an adversary with that access. It holds one
database at a time; the complete NAS
SQLite output also exists until that tar member has been written. Python
exceptions, including KeyboardInterrupt, clean both scratch locations. Process
termination or node loss
can leave local files until Pod deletion, and space checks do not reserve capacity
against other writers. This avoids NAS random reads during online-output
validation, but adds a sequential copy and does not guarantee a completion time.

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
Set `SOURCE_DATA_CLAIM` to the claim mounted at `/data` when selecting a different
Music Assistant data PVC. The worker validates and records this value in the
snapshot manifest; it defaults to `music-assistant-home-data` for existing jobs.

The native restore tool makes its destination owner-only (0700), including a pre-existing empty directory, before reading backup contents. Choose a private parent directory without untrusted concurrent writers.

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

Before writing anything, the installer selects the unique container mounting
`/config`, requires it to be running and ready, and verifies its installed native
backup contract. Both Python workers in the template must use that HA image's
immutable digest; a mismatch requires deliberate template alignment, not an
automatic HA upgrade. The installer server-dry-runs the Secret (when supplied),
code ConfigMap, CronJob and NetworkPolicy together before its first write.

An unfinished Job using the shared code ConfigMap blocks installation, including
pending Jobs that have not started a Pod yet. Pod identity and Job state are
rechecked after admission validation. These checks are not a cluster transaction:
use an exclusive maintenance window without concurrent installers, HA rollouts or
scheduled/manual backup starts. A later apply failure can still leave a partial
update; inspect the resources before retrying.

Recorder staging and export directories must have real, unlinked ancestors
inside the configuration mount. Existing manifests must be regular files with
one hard link. The runner rejects aliases before staging cleanup or metadata
writes, preserving the previous verified export. Metadata is written to a
private, exclusively created temporary file before atomic replacement. Keep the
configuration mount free of concurrent untrusted filesystem writers.

### Separate temporary archive filesystem

When HA mounts `/config/tmp_backups` from a separate host filesystem, the
installer discovers that existing `hostPath` and mounts the same directory
read-only at `/ha-backup-tmp` in the prepare and native-backup containers. It
requires `hostPath.type: Directory`, an exact mount without `subPath`, matching
HA/Job nodes and the same configuration PVC. The optional
`--backup-temp-host-path` argument asserts the expected host path; it never
creates the directory or changes HA's mount.

The installer reads the Linux device number from HA's actual mounted path and
records it as `HA_BACKUP_TEMP_DEVICE`, alongside `HA_BACKUP_TEMP_DIR`. Every
prepare and final capacity check requires that exact mounted device, distinct
from the configuration filesystem. A missing mount, root-filesystem fallback,
symlink or changed device fails closed. After intentionally changing the storage
device, rerun the installer to validate it and refresh the expected number.

For separate filesystems, the export budget is the smaller of configuration
free space minus its reserve, archive free space minus its own reserve and
measured archive overhead, and the existing reviewed SQL ceiling. The ceiling
is still selected from configuration filesystem size. After publication, both
reserves are checked again and the actual SQL size plus archive overhead must
fit on the archive filesystem. The legacy single-filesystem calculation remains
unchanged when HA has no separate mount. These are fresh measurements, not disk
reservations; concurrent backups and unrelated large writes remain unsupported.

If the configuration filesystem drops below its reserve after publication,
the runner fails safely and retains the verified SQL export. Its numeric
`config_capacity` record is currently available only in Job logs; the Core
updater reports a generic Job failure for this case. Archive-filesystem capacity
failures continue to use the existing structured updater diagnostic.

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
