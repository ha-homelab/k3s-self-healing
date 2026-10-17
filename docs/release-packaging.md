# Source release packaging and acceptance

The release remains the original deterministic public-source archive:
`k3s-self-healing-v<package-version>.tar.gz` and its companion `.sha256` file.
It contains exactly the paths in `publication.json`, under the existing
`k3s-self-healing/` directory. The inventory, privacy scan and source syntax
checks run before packaging. No Docker image, cluster
installation, operator backup, private profile or restored system is published.

## Version ownership and frozen inputs

`SOURCE_VERSION` identifies the portable source collection, including the HA
packages and runtime tests maintained here. The Helm chart and upstream Home
Assistant versions remain independent and are not rewritten by source-release
versioning. The policy checks the source version and its actual archived
copy, not merely the asset filename.

The explicit promotion profile is `promote-bytes`: beta 7 uses tag
`v0.1.4-beta.7` and embedded suite version `0.1.4-beta.7`; RC 7 uses tag
`v0.1.4-rc.7` and embedded version `0.1.4`. These are format examples, not claims
that those candidate tags have been published. This allows a later approved stable
promotion to retain exact RC bytes. The full tag, source SHA, manifest and
checksums remain essential because the numeric version does not identify the
individual RC. This onboarding does not publish a stable release.

The allocator freezes a source- and policy-bound `.release-plan.json` before
building. The build applies only its declared version field and records the
effective input digest in `.release-inputs.json`. The existing local packager
still requires a completely clean commit when called without candidate
arguments. Candidate packaging requires the frozen plan, validates its version
overlay and refuses every changed tracked file except `SOURCE_VERSION` and every
unignored untracked file. The complete publication inventory must still match
the checkout. Ignored plan and output files are never archive inputs.

## Build and archive acceptance

The adapter preserves normalized archive ownership, timestamps, file order and
the existing path layout. It verifies the complete final archive against the
approved source inventory and exact overlay bytes, then extracts it to a private
temporary directory. From that extraction it runs the privacy scan,
render and restore help commands, actual native-backup
unit suite and committed fuzz corpus. Help commands do not restore, deploy,
inspect an installed Home Assistant instance or contact an operator service.

The source and pinned Home Assistant runtime jobs remain required. They
validate the checked-in files, render synthetic manifests, scan
source/rendered files for secrets and run offline package scenarios in the
pinned HA image. The Linux RC campaigns additionally exercise the two bounded
pure targets documented in [fuzzing](fuzzing.md). Local unit/corpus replay alone
does not establish a completed mutation campaign.

Staging reads the real archived version and creates a receipt over the archive
and checksum bytes. Publication requires consistent receipts and the immutable
source/plan evidence. Archive verification adds no permission to run cluster or
restore commands. A successful CI result proves the tested software contracts,
not a particular installation's recovery readiness.

## Reproduction and operations

Use a disposable checkout of the accepted manifest's exact source SHA and
restore its saved `.release-plan.json`. With the validation requirements
installed, run the actual base and channel from that plan:

```sh
python3 scripts/version_plan.py sync --plan .release-plan.json
python3 scripts/package_candidate.py 0.1.4 rc
python3 scripts/verify_source.py release-dist/*.tar.gz
python3 scripts/stage_release_assets.py --target package --pattern 'release-dist/*'
```

These commands write and test local artifacts; they do not publish or deploy.
`tools/package_release.py OUTPUT` remains available for ordinary clean-commit
source archives. Only a candidate with a valid frozen plan can receive a release
receipt. Use empty output directories and retain the public manifest and
checksums, not transient checkout state.

For installation acceptance, verify the published checksum and extract to a
new directory. Review [installation](installation.md) and
[recovery](recovery.md) against a separate test environment and private site
profile. A maintainer must record the exact candidate tag, source SHA, hashes,
installation results and unresolved limitations before stable approval. No live
backup, database restore, update approval, Kubernetes mutation or physical
hardware acceptance is performed by this release workflow.

Any source, policy, recipe or dependency correction requires a new candidate.
Do not replace published assets, rewrite old tags or reset sequence state. A
failed allocation may leave a gap. Rollback uses a previously accepted immutable
source release and compatible private configuration. See [version plans](VERSIONING.md)
and [the release runbook](release-workflow.md) for the shared lifecycle contract.

## Public-source boundary

This checkout belongs to the independently initialized public repository. It
must never merge private predecessor history or copy operator state. All shared
tooling and generated contract tests are explicitly listed in `publication.json`
and receive the same privacy scan. The only added scanner allowance is the
reserved synthetic `example.test` email domain used by a version-parser test;
real email domains and nonregular/binary source inputs remain rejected.

The complete archive acceptance suite runs on Python 3.14, matching the public
source and release-build jobs. `.release-policy.json` sets
`release_contract_python` to `3.14`; this preserves native capacity checks that
use `Path.full_match` rather than skipping them under an older interpreter.
