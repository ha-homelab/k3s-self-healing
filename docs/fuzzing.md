# Bounded backup contract fuzzing

See the [illustrated RC fuzzing process and verified rollout results](https://github.com/victron-venus/venus-os-ci-toolkit/blob/main/docs/rc-fuzzing-results.md)
for the shared release flow, evidence requirements and a dated cross-project
snapshot. The project-specific scope and reproduction guidance below still apply.

The targets exercise the repository-owned pure functions `validate_metadata`,
`known_dump` and `validate_snapshot`. They never import a Home Assistant runtime,
read an installed source tree, open a backup archive, connect to Kubernetes or
contact a NAS. The committed corpus contains synthetic recorder metadata and
mutation descriptions, with no installation configuration, database content,
credentials, host addresses or device identifiers.

`recorder-metadata` feeds arbitrary bounded JSON to the real manifest validator.
Accepted metadata must use a recognized basename, exact integer format and
positive exact integer byte counts. Independently invalidating its checksum,
filename, byte counts or table inventory must then make validation fail.
`runtime-contract` mutates the public reviewed source fingerprints, dependency
inventory and Python/version claims. Successful results must still match a whole
reviewed source profile; deleting a required file must revoke eligibility. The
reviewed upstream hashes are already public source data, not operator state.

The checked-in target budget is 60 seconds each, with a 4096-byte input limit.
The runtime and dependency lock are in `.github/fuzz.json` and the adjacent
hash-locked requirements. Atheris 3.1.0's pinned wheel is Linux/x86-64 only. Use
the shared Linux runner for mutation testing; do not substitute an unpinned
local engine when a wheel is unavailable.

The shared RC workflow runs both Linux mutation campaigns and binds their
immutable result to the exact candidate source and frozen version plan. Missing
or failed results prevent RC publication. Ordinary source validation also runs
deterministic corpus replay through `tests/test_fuzz_oracles.py`; this replay is
not a mutation campaign and cannot qualify an RC by itself.

For portable local replay:

```sh
python3 -m unittest discover -s tests -p test_fuzz_oracles.py -v
```

The existing source/privacy checks and full offline/runtime test suites remain
required. Passing these targets does not establish live backup completion,
restore success, storage capacity, HA compatibility beyond the reviewed profile,
or installation approval. Those require the documented installation acceptance
procedure using the exact candidate and separately retained operator evidence.


## Deterministic preflight in ordinary CI

For relevant changes in a public repository, the existing CI configuration
contract job installs the hash-locked Python 3.12 fuzz dependencies, runs
`pip check`, and replays the committed corpus with Atheris `-runs=0`.
This is deterministic seed replay: it does not start the timed mutation
campaign. Timed fuzzing remains exclusive to release candidates. The shared
preflight has a four-minute total budget, including dependency setup.

The `preflight-paths` entries in `.github/fuzz.json` select the relevant
production, dependency and harness changes. Shared contract, manifest and
workflow changes also trigger this check; missing comparison history runs
the check conservatively. Unrelated changes can skip this extra replay.

Before replay, the shared helper verifies that the following declarations
refer to project-local Python functions already instrumented by the
harness. Missing import instrumentation fails the check instead of
silently counting executions with incomplete feedback:

- `native_backup.dump_format.validate_metadata`
- `native_backup.dump_format.known_dump`
- `native_backup.contract.validate_snapshot`
- `native_backup.contract.source_digest`

Update these declarations when production functions move or are renamed.
They prove that the selected code is instrumented; they do not prove
exhaustive path coverage. Keep positive and rejected-input corpus cases
and independent semantic assertions alongside the existing unit tests.

## Artifact selection and retries

Release artifacts use `-run-<run_id>-attempt-<run_attempt>` names, and the collector selects only the current run and attempt.

Before publication starts, use **Re-run all jobs** only if the allocator and
release policy permit reuse of that run's reservation. Recreate every required
input in the same attempt; failed-only or individual retries can leave inputs
missing, and older artifacts must not fill those gaps.

For a fresh candidate or final build, once publication has started
(`begin_publication`), or its external publication result is partial or uncertain,
inspect the existing release, tags and assets before retrying. A consumed build
number requires a new workflow run and reservation authorized by the release
policy; an existing immutable tag may also require a new base version. Never
reset the release ledger, overwrite assets or retag to force a retry. Historical
byte promotion of an accepted RC keeps its reserved number and follows the
existing promotion policy.

Fuzzing still requires an explicit RC dispatch from the default branch of a public repository. Its report identifies one source revision and run; it does not prove complete platform coverage or device acceptance. Package receipts additionally bind fresh package publication to the current repository, source SHA, run ID and attempt. Historical promotion and retained-evidence validation keep their existing checks; complete package-set coverage remains a separate requirement. See [the shared evidence contract](rc-fuzzing.md).

### Package receipt identity

Fresh publication verifies each staged package receipt against the independently
supplied repository, source commit, workflow run and attempt. Receipts created
before this migration, or offline without an Actions identity, cannot qualify a
fresh publication. Run all required producers in one complete workflow attempt
after migration; an earlier attempt cannot fill missing inputs.

Historical promotion and retained-evidence validation keep their existing checks.
Receipt coverage validates the staged files; the expected packages and platforms
still require a separate completeness check. Existing release approval and device
acceptance policies continue to apply.

See the shared [receipt migration guide](https://github.com/victron-venus/venus-os-ci-toolkit/blob/main/docs/release-workflow.md#migrate-package-receipt-identity-without-replacing-the-release-engine).

### Complete package producer inventory

`versioning.required_build_receipts` declares the exact package and signature
receipt filenames required by this project's final publisher. Each authenticated
receipt must match the repository, source commit, workflow run and attempt. A
missing producer or an unexpected receipt stops fresh publication, even when the
remaining packages are valid. RC fuzz evidence is checked separately; pre-signing
checks do not require a signature receipt that has not been created yet.

Before publication begins, rerun all required producers only when the allocator
and release policy permit reusing that reservation. After publication begins or
its outcome is uncertain, inspect existing tags, releases and assets before a
policy-authorized new run; never reset the ledger or mix earlier-attempt packages.
Historical RC promotion keeps its accepted source policy. Keep this explicit
inventory aligned with producer changes; it does not replace package-format,
signature or device acceptance checks.
