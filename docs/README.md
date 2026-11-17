# k3s self-healing — documentation index

This index covers the portable Home Assistant Container stack on k3s: deployment
recipes, approval-gated Core updates, verified backups, recorder guards and the
recovery tooling around them. Start with the visual
[process and evidence guide](process-and-evidence.md) for the workflow and its
verification limits.

## Get started

- [Installation](installation.md): prerequisites, kubectl context selection and
  the base stack commands.
- [Process and evidence](process-and-evidence.md): the illustrated approval and
  update flow, including what private evidence cannot prove.
- [Public source policy](publication.md): what is published and what stays in the
  private installation.

## Operate

- [Maintenance](maintenance.md): Core updates, HA packages and private approval
  receipts.
- [Stable Core updates through a reviewed pull request](core-update-proposals.md):
  the optional proposal mode ahead of the explicit approval.
- [Source candidate lifecycle](release-lifecycle-design.md): how a candidate
  moves to a packaged release.

## Recover

- [Backup and recovery](recovery.md): backup sources, restore paths and rollback.
- [Recorder storage backpressure](recorder-storage-budget.md): the 5 GB operating
  budget and how the updater supervises it.
- [Recorder orphan deletion guard](recorder-orphan-guard.md): avoiding MySQL
  error 1451 during a native purge.

## Reference

- [Independent review and protected merge](REVIEW_AUTOMATION.md): how the shared
  workflows read GitHub metadata.
- [Quality evidence and current limits](QUALITY.md): scope of the deployment and
  what the public checks do not cover.

## Development and release

- [Source release packaging and acceptance](release-packaging.md).
- [CI and release operations](release-workflow.md).
- [Release read recovery and rerun validation](release-read-safety.md).
- [Bounded backup contract fuzzing](fuzzing.md) and [RC fuzzing](rc-fuzzing.md).
- [Early final-build runner check](runner-preflight.md) and
  [version plans](VERSIONING.md).

Shared repository policy (`release-workflow.md`, `quality.md`, `fuzzing.md`,
`rc-fuzzing.md`, `runner-preflight.md`, `release-read-safety.md`,
`VERSIONING.md`) comes from the shared release toolkit; those files are
maintained centrally and are summarized rather than repeated here.

Deployment diagrams under `diagrams/` illustrate the logical boundaries. They
are not measurements or a guarantee of the private installation's state.
