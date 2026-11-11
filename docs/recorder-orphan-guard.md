# Recorder orphan deletion guard

A native purge can abort with MySQL error 1451 when its candidate attribute or
event-data row remains referenced. Because each purge cycle is a transaction,
that error rolls the cycle back and retention stops making progress. A success
response from the purge service only acknowledges queued work.

`helm/recorder-orphan-guard/patch.py` adds an atomic `NOT EXISTS` condition to the
native deletes of `state_attributes` and `event_data`. Each candidate is checked
again by the DELETE itself. Referenced parents remain intact, and foreign keys
stay enabled. Native batching, transaction scope, retention cutoff, state/event
selection and statistics handling are unchanged.

The guard accepts only the exact reviewed HA 2026.11.0/2026.11.1 `queries.py`
SHA-256 `4267c337433edc3f984cbb485b408e83e3ecd8e24d6a078d17330b937cd0ff6c`.
The patched digest is
`93262d72d6547ec341ac0d27c2703ec868c36584ddf404630f77e6e774d0933c`.
It is idempotent and leaves any other upstream source untouched, with a warning.
Review that warning before relying on automatic purge after a Core upgrade;
an unrecognized version has not silently received this workaround.

## Qualification and limits

Isolated MySQL 26.10.0-cloud fixtures reproduced a stale REPEATABLE READ snapshot:
the old unguarded DELETE failed with 1451, while the guarded DELETE retained the
newly referenced parent. Separate attribute and event-data fixtures also passed
pending-writer commit/rollback races, rejection of a late child insertion,
mixed referenced/orphan candidates and 1/999/4000-ID batches. Each disposable
schema was removed after testing. No production foreign key was disabled by
these tests.

The source tests exercise both real SQLAlchemy 2.0.54 statement compilation and
cached executions, source-version refusal, idempotence and atomic replacement.
Related [HA issue 170977](https://github.com/home-assistant/core/issues/170977)
and [issue 177871](https://github.com/home-assistant/core/issues/177871) describe
the same failure class. The fixture proves the guard's tested semantics; it does
not identify the concurrent writer or prove that snapshot timing caused every
production 1451 error.

The native cache eviction can discard a candidate that the guard retains. This
only removes the in-memory lookup: a later lookup can find the preserved database
row again. It does not delete that row or its child references. Deadlocks or
other database errors retain the native transaction/retry behavior.

## Activate and verify

1. Apply `kubectl apply -k helm/recorder-orphan-guard` in the reviewed cluster.
2. Include the matching ConfigMap volume and `/etc/cont-init.d/53-recorder-orphan.py`
   mount from `helm/recorder-orphan-guard/values.yaml` in the maintained Helm release inputs. Merge these entries with existing volume/mount lists; Helm list overrides must not discard other guards.
3. Coordinate one ordinary HA restart. Never replace source beneath a running
   interpreter or during an active Core update/backup.
4. Check the new container logs for `recorder-orphan-guard: patched` or
   `already-patched`, and confirm the installed target digest above.
5. Queue one native purge with the intended retention policy and verify committed
   progress and its final logs. Do not interpret changing InnoDB estimates or
   service HTTP 200 as completion. A repack is separate and requires enough disk.

The native-recovery chart uses an init container and read-only patched-source
mount. `scripts/restore-native-backup.py` creates the same guard ConfigMap, so
restoration does not silently omit it. Shared source remains covered by the
public dependency release and lock; publish and pin that release before treating
private validation as complete.

Remove this guard after a reviewed upstream fix replaces it. Upgrading from
2026.11.0 to 2026.11.1 alone does not replace the reviewed recorder implementation.
