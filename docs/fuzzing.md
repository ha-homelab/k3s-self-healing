# Bounded backup contract fuzzing

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

The target configuration is prepared for release-candidate lifecycle integration.
The current validation workflow runs deterministic corpus replay through
`tests/test_fuzz_oracles.py`; it does not start a mutation campaign. A corpus
replay is not a Linux fuzz qualification result. The future RC workflow must bind
its actual result to the candidate source, frozen version plan and artifact
receipt before publishing the candidate.

For portable local replay:

```sh
python3 -m unittest discover -s tests -p test_fuzz_oracles.py -v
```

The existing source/privacy checks and full offline/runtime test suites remain
required. Passing these targets does not establish live backup completion,
restore success, storage capacity, HA compatibility beyond the reviewed profile,
or installation approval. Those require the documented installation acceptance
procedure using the exact candidate and separately retained operator evidence.
