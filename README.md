# Home Assistant on k3s: recovery and maintenance

[![Fuzzing: RC only](https://img.shields.io/badge/fuzzing-RC_only-blue)](docs/fuzzing.md)

Portable Home Assistant Container deployment recipes, companion services,
approval-gated Core updates, verified backups, and recovery tools.

This public source tree contains generic code and synthetic examples. An
installation's addresses, storage locations, notification recipients, credentials,
device identifiers, and recovery archives belong in its private configuration.
GitHub authorship and device model names are intentionally public.

## What is included

- A pinned Home Assistant Helm configuration and authenticated Mosquitto recipe.
- Optional ESPHome, Ring-MQTT, and Music Assistant companion recipes.
- A separate Core update controller: exact-version approval, backup verification,
  target-image configuration preflight, health checks, and an interruption journal.
- Streaming MySQL recorder export followed by a verified native HA backup.
- NAS recovery sets with SQLite consistency handling, hashes, bounded retries,
  safe extraction, and a stopped recovery chart.
- HA Core approval and HACS maintenance packages, optional notifications, and
  a motion-light blueprint that recovers missed shutdowns after unavailability.
- Offline fault-injection tests, HA runtime tests, source privacy checks, and
  deterministic source packaging.

Kubernetes restarts failed Pods. Recovery from a lost node or damaged persistent
storage is an operator procedure; one replica with local-path storage is not an
automatically failing-over HA cluster. Do not run two Home Assistant instances
against copies of the same live pairing state.

## Validate a fresh checkout

Use Python 3.14, Helm 3, and kubectl with Kustomize support.

```sh
git clone https://github.com/ha-homelab/k3s-self-healing.git
cd k3s-self-healing
python3.14 -m venv .venv
. .venv/bin/activate
pip install -r requirements-validation.txt
python tools/validate.py
python -m unittest discover -s tests
helm lint helm/native-recovery
```

`runtime_tests/` additionally needs the pinned Home Assistant image. The CI
workflow runs it without network access, using only synthetic services and state.

## Deploy the base stack

This is an installation recipe, not a copy of an existing home's configuration.
See [installation](docs/installation.md) for prerequisites and exact commands.

1. Copy `examples/site.example.json` to ignored `local/site.json`. Replace the
   example node names, IPs and storage settings with your own values.
2. Create a Mosquitto password file locally and install it as the
   `homeassistant/mosquitto-passwd` Secret (`password.txt` key).
3. Render the companion manifest and Helm values with `tools/render.py`.
4. Apply them to an explicitly selected Kubernetes context, and complete HA's
   onboarding through a local port-forward.

The default stack uses Pod networking and exposes only ClusterIP services.
LAN discovery, USB radios, ingress, TLS and external MQTT access are installation
choices; configure them in private overlays. Optional companion manifests are
not installed by the base Kustomization.

## Maintenance and recovery

- [Core updates and HA packages](docs/maintenance.md)
- [Backup, storage and recovery](docs/recovery.md)
- [Publication and privacy policy](docs/publication.md)
- [Security and reporting](SECURITY.md)

The native recorder backup and automatic Core updater currently implement the
MySQL + Synology DSM backup contract. They are optional and require that backend,
the documented HA backup destination, credentials, and sufficient temporary
space. The base HA deployment can use its ordinary SQLite recorder.

Do not install backup or update workers until their prerequisites are satisfied.
The updater writes image pins and approval receipts to the operator's selected
deployment repository. Use a private deployment repository for those receipts.

## Source release

```sh
python tools/package_release.py build/k3s-self-healing-source.tar.gz
```

Packaging requires a clean commit and an exact match between tracked files and
`publication.json`. The archive contains no Git directory, user/group names,
workstation timestamps, private source revision mapping, or runtime data.

Original project code is MIT licensed. See `NOTICE` for upstream dependencies.

## RC fuzz qualification

The published [v0.1.2-rc.1 release](https://github.com/ha-homelab/k3s-self-healing/releases/tag/v0.1.2-rc.1)
contains the qualification report for that tag's exact source commit.

[Bounded backup contract targets](docs/fuzzing.md) qualify release candidates by
mutating synthetic metadata and reviewed profiles through the pure validators.
The badge identifies RC-only capability; reports bind qualification to the source
commit. See [the release lifecycle](docs/release-lifecycle-design.md) for the
source release boundaries and separate installation acceptance.

## Source candidate releases

Versioned beta/RC archives retain the exact public-source inventory and normalized
archive layout. See [packaging and acceptance](docs/release-packaging.md) for
frozen version plans, isolated source checks and separate installation acceptance.

## Project maintenance

See [contribution and testing policy](CONTRIBUTING.md), [conduct and confidential contact](CODE_OF_CONDUCT.md), [governance](GOVERNANCE.md), [roadmap](ROADMAP.md) and [security limits](SECURITY.md). OpenSSF Silver continuity is not established; full network-transport qualification remains incomplete.
