# Quality evidence and current limits

The source is a networked Home Assistant/k3s maintenance and recovery system. Its complete deployment includes privileged backup and update workers, the approval API, HA/MQTT, Kubernetes and external tools. An isolated archive helper is useful, but does not qualify the whole deployment's cryptographic configuration.

On 2026-10-16, the source suite ran 329 tests: 328 passed and one optional platform case was skipped. Sixteen workflow-contract tests passed and Helm lint accepted the recovery chart. Real local HTTP tests cover successful requests and all five redirect statuses for both administrative urllib clients. Real WebSocket tests cover the updater, native-backup and Kubernetes clients: all five redirect statuses are rejected before contacting the destination or sending an authentication message. Direct authentication still works. A regression also requires an existing shared native-restore directory to become 0700 before backup parsing. Archive and restore tests cover paths, links, truncation, SQLite consistency, changed source trees, private modes and interrupted operations. These are synthetic tests, not deployment or physical recovery acceptance.

CodeQL Python/Actions analysis is part of the required quality gate. The existing source inventory, privacy scan, syntax checks, HA source-import verification, release contracts and pinned HA runtime tests remain enabled. All maintained source is in scan scope; an intentional test fixture needs a narrow documented disposition rather than an excluded production path. A raw Ruff security inventory is supplementary review input, not a claim that every generic warning has been eliminated.

The required source validator also runs pinned Ruff 0.16.5 diagnostics
`E9,F63,F7,F82` over every Python file in the publication inventory. These detect
syntax/basic control-flow errors and undefined names. Only the exact Home
Assistant `python_script` entry point receives its three documented injected
names (`data`, `hass`, `output`); other unknown names still fail. Full formatting
and every optional security/style rule are not asserted to pass.

The local Python environment audit on 2026-10-16 found no published vulnerabilities in the 11 installed validation/runtime-client packages. This does not audit every package in the pinned Home Assistant container, Helm/kubectl, SSH/MySQL/NAS dependencies or an operator's deployment. Full runtime dependency and TLS key-policy qualification remains incomplete, so no whole-project Passing vulnerability or crypto guarantee is inferred from that small clean inventory.

The 2026-10-16 runtime image review found that the updater and recovery worker
still used Home Assistant 2026.9.1. Maintained HA manifests now select the
2026.10.2 multi-platform digest, also tested by CI. The native backup contract
checks actual installed upstream source hashes and dependency versions; it is
not bypassed by the image update. Trivy 0.74.0's Linux/amd64 inventory of the new
image still reports 259 finding rows (165 distinct IDs) of medium or higher
severity published before 2026-08-18. These are inventory findings, not 165
proven exploitable application paths. In particular, bundled OpenSSL, SSH and Go
binaries need further applicability review and fixes. Updating HA alone is not
a clean-image attestation. Reproduce the raw inventory with:

```sh
trivy image --image-src remote --scanners vuln --platform linux/amd64 \
  ghcr.io/home-assistant/home-assistant:2026.10.2@sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4
```

Password verification has a separate, narrower scope. Home Assistant's
[2026.10.2 provider](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/auth/providers/homeassistant.py)
uses a fresh bcrypt salt and cost 12. The installation recipe explicitly selects
salted PBKDF2-HMAC-SHA512 at 220,000 iterations for the pinned Mosquitto 2.1.2
broker. A network-isolated invocation of that exact broker image accepted the
correct synthetic password and rejected an incorrect one. Its
[implementation](https://github.com/eclipse-mosquitto/mosquitto/blob/v2.1.2/libcommon/password_common.c)
generates per-password salts with OpenSSL RAND_bytes. This evidence covers
inbound password verifiers, not network encryption or storage of outbound
service credentials.

Release-candidate fuzz reports apply to their recorded source commits. The current source has not acquired new fuzz coverage merely by adding unit tests. Silver continuity remains unmet because no backup maintainer or one-week access recovery mechanism exists.
