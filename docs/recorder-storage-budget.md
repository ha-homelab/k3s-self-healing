# Recorder storage backpressure

The existing Core updater can supervise a **5 GB operating budget** for the Home
Assistant Recorder schema. This is not a MySQL quota or a guaranteed physical cap.
The guard uses the existing HA image, read-only configuration mount, HA API token,
and durable updater state volume. No second database credential, CronJob, service,
PVC, or Kubernetes privilege is required.

The source is opt-in. Adding these files does not enable a live guard. Enable it
only after the initial history cleanup, with the actual schema and a reviewed
single-host MySQL network destination in private updater settings.

## Policy

- Measure every five minutes, with bounded read-only SQL. Refresh MySQL metadata
  rather than accepting its default 24-hour statistics cache.
- Keep one day of raw history. When the native purge path has been qualified,
  request `recorder.purge` at most hourly, or at most every 15 minutes from
  **3.5 GB**, if rows older than one day exist. `repack` and `apply_filter` stay
  false. The HA configuration must also use `purge_keep_days: 1`.
- At **4.5 GB**, persist a pause receipt before requesting `recorder.disable` and
  create one stable persistent notification. Reapply the pause every five minutes,
  including after an HA/updater restart or a failed database measurement.
- Do not automatically call `recorder.enable`. A lower measurement does not clear
  the durable pause. Native HA has no supported public API that proves whether an
  operator independently disabled Recorder; the guard must not undo that choice.
- Save one purge request before HTTP dispatch. A timeout is an unknown outcome,
  not permission to retry. Completion requires every inspected table's oldest row
  to pass that request's **fixed cutoff**. No cutoff progress for 30 minutes raises
  a warning; it never floods Recorder with duplicate purge tasks.

Recorder's automatic daily purge remains enabled and has its own scheduling. The
guard avoids repeating its own queued/unknown request and checks the existing
backup/HACS/updater maintenance state before an extra request. A shared admission
lock serializes the durable purge intent against Core approval; Core approval
refuses an unresolved purge receipt. Updater `recovery_required` blocks extra
purges but does not block size measurement, backpressure, or HA's own daily purge. It cannot prove
that the native daily purge is inactive; these supported tasks share Recorder's
queue. The extra controller does not execute direct DELETE, TRUNCATE, OPTIMIZE,
ALTER, or schema migration statements.

## What the measurements mean

`DATA_LENGTH + INDEX_LENGTH` is table/index allocation, not an exact byte count of
live rows. `DATA_FREE` is reported separately and is never subtracted to declare
space reclaimed. A required read-only `recorder_storage_probe()` stored procedure exposes only
this schema's `INNODB_TABLESPACES` totals through columns `schema_name`,
`allocated_bytes`, and `file_bytes`. A DBA creates this SQL SECURITY DEFINER procedure
using the exact schema comparison and file-per-table spaces; no administrator
credential or broad PROCESS grant is added to HA or the updater. The guard uses
the larger of data/index allocation and physical `ALLOCATED_SIZE` for thresholds
and reports `FILE_SIZE` separately. A missing/unreadable procedure or wrong schema is
a measurement failure, never a claim that physical usage fits the budget.
The generic DBA setup is [recorder_storage_probe.sql](../core_updates/recorder_storage_probe.sql).
Review both schema literals and the DEFINER identity, then verify the call using
the actual HA account. A view can still require the invoker to hold PROCESS on
some managed MySQL versions; the qualified procedure avoids that broad grant.
The client drains all CALL result sets before reusing or closing its connection.

InnoDB may retain deleted pages for reuse. A successful purge therefore need not
shrink tablespace files. Reclaiming physical space requires a separately reviewed
operation with enough temporary capacity. Hourly long-term statistics, metadata,
configuration, devices, and unrelated schemas must remain intact. Preserve the
latest five-minute statistics too: HA uses their `state`, `sum`, and `last_reset`
as the baseline for cumulative Energy statistics.

`recorder.disable` suppresses raw event/state recording. Statistics, imports,
queued work, MySQL logs, and other schemas can still write. Polling has a five-minute
interval and requires available HA/network services; it is not a hard stop at an
exact byte. With a durable pause active, an HA restart can resume raw writes until
the next successful guard call. Investigate any continuing growth rather than
calling the schema physically capped at 5 GB.

## Enable in private updater settings

Merge this object into the existing settings file; the address below is an
example, not an operational endpoint. Do not put the database URL or HA token in
this object or in the public repository.

```json
{
  "recorder_budget": {
    "enabled": true,
    "schema": "homeassistant",
    "database_ipv4_cidr": "192.0.2.7/32",
    "database_port": 3306,
    "purge_qualified": false
  }
}
```

Use the existing `scripts/install-core-updater.py` workflow. Its ConfigMap includes
the new module automatically, and its checksum rolls only the updater. The
installer adds MySQL egress only for the exact `/32` and port. The mounted Recorder
configuration must use `db_url: !secret recorder_db_url`, directly or in its
included recorder file. That secret's database must match `schema`; a schema
containing `kine` is rejected. A separate schema/account remains required if k3s
shares the MySQL instance. The destination must match the actual database route;
if its address changes, review and update the private `/32`.

Leave `purge_qualified: false` while investigating a foreign-key purge failure.
The size measurement and pause still operate, but no extra purge is queued. After
a real purge succeeds on the deployed runtime with constraints intact, set it to
true. A service response alone is not proof of success.

## Verify and recover

1. Read private `/state/recorder-budget.json` in the updater container. Confirm
   fresh `snapshot.measured_at`, the expected allocation, and physical-status
   wording. Logs expose only error classes, never credentials or raw SQL errors.
2. If `pending` is present, inspect HA Recorder logs and fixed-cutoff progress.
   Do not delete the receipt to force another request. Resolve the prior outcome
   first. The 4.5 GB pause remains eligible even during pending cleanup.
3. For a pause, keep it latched until the cleanup fault is fixed, storage is
   confirmed below **3.5 GB**, and the operator confirms it is appropriate to
   resume raw history. Check that the receipt belongs to this guard/target;
   an unrelated manual pause must remain respected.
4. In a coordinated maintenance window, disable this guard in private settings,
   roll only the updater, preserve its JSON receipt, and verify no guard worker
   is running. Then remove only the resolved `pause`/`pending` fields from the
   state file while retaining the receipt separately. Call HA's supported
   `recorder.enable` only as the explicitly reviewed recovery action. Re-enable
   the guard with the qualified purge setting and verify fresh samples.

If the database is unavailable or a read fails, no cleanup request is made. An
existing pause is still retried. If no pause had yet been triggered, a monitoring
failure raises a persistent notification; it is not evidence that the budget is
safe. No privileged database reclamation is attempted automatically.

References: [Home Assistant Recorder](https://www.home-assistant.io/integrations/recorder/),
[HA 2026.11.0 Recorder services](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/recorder/services.py),
[HA cumulative statistics baseline](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/sensor/recorder.py),
[MySQL table statistics](https://dev.mysql.com/doc/refman/8.4/en/information-schema-tables-table.html).
