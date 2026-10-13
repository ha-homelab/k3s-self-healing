# Planned source candidate lifecycle

The existing deliverable is the deterministic, explicitly allowlisted source
archive produced by `tools/package_release.py`. The next source base should be
0.1.2, following the already published 0.1.1 stable release. This source-bundle
version will identify the portable tool collection; it must not overwrite the
upstream Home Assistant version, imported-source lock, or the independently
versioned Helm chart.

Adoption will add a dedicated source version field and an archived copy of that
field, with frozen beta/RC projections and a receipt over the exact archive bytes.
The adapter must preserve `publication.json`, privacy review, imported HA source
integrity and deterministic archive layout. The current clean-commit requirement
must permit only the declared temporary version overlay in a verified frozen
plan, while rejecting all other changed or untracked source. Direct local source
packaging without a plan must keep requiring a clean reviewed checkout.

The current `validate.yml` source and pinned offline HA runtime jobs must remain
required. The shared lifecycle can call that validation without duplicate push/PR
runs, but must not replace its secret scanning, upstream-source reproduction,
synthetic rendering or runtime tests. Acceptance of the final archive should
verify every allowlisted file and run source-tool help/import checks from an
isolated extraction; no deploy, install, restore or update command may execute.

Only Linux RC qualification should run the two bounded mutation targets described
in `fuzzing.md`. Beta publication uses the same immutable source packaging and
validation requirements. Publication will use the shared toolkit's durable merge
commit, not a pull-request-only SHA. No stable promotion, existing release change
or operator-system access is part of this adoption work.
