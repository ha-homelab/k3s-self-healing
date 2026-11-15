# Optional Kine MySQL capture source

This directory contains source for an optional encrypted capture helper. The
source release does not publish a helper binary, install a database client, or
configure a cluster. Its tests use synthetic SQL and a temporary shell fixture;
they do not qualify an installed database, its schema, a restore, or a recovery
point objective.

The helper supports a deliberately narrow profile: one `kine` table with the
nine-column schema and required indexes from [Kine v0.13.5][kine], emitted by
MySQL 8.4.10 `mysqldump` with the helper's fixed options. It requires the exact
ordered session setup and restoration statements from the pinned client's
[header, table and footer code][dump]. Unknown statements, changed schemas,
unrecognized table options, extra executable version comments, missing indexes,
and SQL after the terminal completion marker fail closed. This is not a general
SQL parser or an automatic backend detector.

The accepted schema includes the known historical signed `bigint` spelling,
optional migration `UNIQUE KEY id`, and the finite charset/collation forms
covered by the tests. These compatibility cases are source contracts; no actual
`SHOW CREATE TABLE` output or operational dump is included in this repository.
Accepted collation names use restricted identifier/prefix forms within those
charset families; the parser does not qualify every server collation name.
Before adoption, an operator must independently qualify the actual schema,
client image, TLS route, privileges, resource limits and restore procedure.
An unsupported schema must receive a reviewed source change and fresh
qualification rather than bypassing the parser.

## Capture contract

The client image identity is
`docker.io/library/mysql:8.4.10@sha256:8dbcf531a03aade657e181b9cf2f1d1803ce621a1d55610cb44cb531ab7d7db6`.
The helper checks the invoked client's version string; that alone does not
attest the executable bytes. Installation tooling must bind the actual client
and image to this identity. The source archive does not perform that deployment.

Private configuration supplies the fixed loopback TLS endpoint, schema,
read-only user, CA path and exactly two distinct age X25519 recipients. It must
be a private regular file. Credentials stay out of command arguments and normal
logs; a temporary client options file is removed on exit. The client receives
`VERIFY_IDENTITY`, `--single-transaction`, explicit complete columns, primary-key
ordering, hexadecimal BLOBs and disabled triggers/locks. Passwords and row data
must never be put into repository examples or CI logs.

SQL streams directly through a typed-row validator, gzip and age encryption.
The typed proof distinguishes NULL, empty values and BLOB bytes, requires
monotonic unique row IDs, and checks compaction/revision/next-ID bounds. The
source and typed-row digests describe only the accepted dump. They do not prove
that another service can restore it or that the database is healthy.

The implementation limits a SQL line to 80 MiB, encrypted output to 2 GiB, and
requires at least 128 MiB free on the output filesystem at each encrypted write.
The client has a 600-second context and bounded wait delay; this is not a claim
that every filesystem operation has a hard real-time bound. Output must be a
new directory. On success it contains only `capture.json` and
`k3s-mysql.sql.gz.age`; partial output is removed on failure. A failed capture
may write a private diagnostic beside the output, which must stay out of public
artifacts and source archives.

## Recovery runner integration

`disaster_recovery.mysql_capture` supports only the managed endpoint assignment
at `etc/rancher/k3s/mysql-datastore.env` beneath the runner's mounted host root,
with the optional `92-kine-mysql.conf` service dependency marker. It recognizes
the fixed loopback route and validates the schema name without exposing the
credential-bearing endpoint. Missing configuration with no marker retains the
existing SQLite profile; an unsafe or dangling managed path is an error.
Custom service configuration and other external database layouts are outside
this profile and need separate explicit discovery/installation support.

When that managed MySQL endpoint is present, recovery requires the optional
`k3s_mysql_capture` setting and the reviewed `/kine-capture/ready` mount. The
runner validates a fresh bounded receipt and encrypted payload before accepting
the component, records its proof, and excludes stale SQLite state files from the
K3s server directory. No checked-in profile enables the option or adds a client
container. Installation-specific manifests, private CA material, recipient
configuration, scheduling and restore approval belong to the operator.

Receipt, ciphertext and directory descriptors stay open while the component is
archived. File/name/root and parent-directory version checks must pass before
the component enters accepted snapshot metadata. The capture parent must remain
stable too: concurrent sibling-entry changes or a parent rename/restore
conservatively refuse acceptance. Early validation and context entry each hash
the ciphertext once; context exit checks metadata/bindings without another full
hash or ciphertext copy. This protects source custody and is not an atomic
cross-service database snapshot.

## Source checks

From this directory, `go test -mod=readonly -count=1 -timeout=120s ./...` runs the
software fixtures, including a fake-client encrypt/decrypt round trip for both
recipients and strict parser refusal cases. The public validation workflow runs
these checks with its pinned Go toolchain. Python receipt and runner tests run
through the existing unittest discovery. Passing source checks must not be
reported as database, backup installation or restore acceptance.

[kine]: https://github.com/k3s-io/kine/blob/v0.13.5/pkg/drivers/mysql/mysql.go
[dump]: https://github.com/mysql/mysql-server/blob/mysql-8.4.10/client/mysqldump.cc
