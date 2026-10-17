# Security

Report vulnerabilities through this repository's private GitHub security
advisories. Do not post credentials, live backups, home addresses, device IDs or
logs containing personal data in public issues. Provide a synthetic reproducer.

Public CI uses GitHub-hosted runners with read-only repository permissions.
No deployment credential or home-network runner is required. Runtime tests use
network-disabled containers and synthetic Home Assistant state.

Backup and update workers require narrowly documented privileged access: the
backup worker reads host k3s/PVC state and Kubernetes Secrets; the updater can
write namespace-scoped Helm Secrets and update the selected HA StatefulSet.
Treat their namespaces and service accounts as trusted administrative components.
Do not give untrusted tenants permission to replace their code ConfigMaps or
credentials. NetworkPolicies require enforcement by the cluster's network stack.

Keep production credentials in local files and Kubernetes Secrets. The profile
renderer contains installation identifiers but does not accept inline credentials.
No source scanner can make a real HA backup suitable for public distribution.

## Supported scope and response

Current main and the latest stable source release receive security fixes. Older installations should upgrade or assess the relevant patch. On 2026-10-16 private GitHub vulnerability reporting was enabled and independently read back. You may also send a confidential report to alvit.work@gmail.com.

We aim to acknowledge a report within seven days, investigate promptly and prioritize critical issues immediately. Coordinate a fix and disclosure date with the reporter; credit the reporter unless anonymity is requested. Release notes identify assigned vulnerability IDs and affected versions. No vulnerability reports were received outside GitHub in the preceding twelve months as confirmed by the maintainer.

The urllib administrative Kubernetes and Home Assistant HTTP clients do not follow redirects or inherit proxy configuration. A selected endpoint must answer directly; a redirect fails before credentials can be sent to a new origin. This does not make an HTTP endpoint encrypted.

The Home Assistant updater, native-backup and Kubernetes WebSocket clients also reject redirects before
contacting the target. In particular, a redirected WebSocket must not receive the
Home Assistant authentication message or a Kubernetes checkpoint command.
Configure the final service URL directly; redirects are rejected even within the
same origin. Normal certificate verification and the overall operation timeout
remain enabled.

## Current deployment limits

The documented inbound password verifiers are Home Assistant's salted bcrypt
(cost 12) and Mosquitto's salted PBKDF2-HMAC-SHA512 (explicitly selected at
220,000 iterations in the installation guide). Reset legacy or weaker Mosquitto
credentials with that guide to upgrade their stored work factor; preserve an
existing stronger verifier instead of downgrading it. Outbound
service passwords and bearer tokens are separate credentials, kept in Secrets;
they are not compared against user-password hashes by this project.

The default private-cluster recipes include HTTP Home Assistant and approval APIs and MQTT without TLS. Kubernetes uses its service-account CA, but the full collection of Python, Helm, kubectl, registry, MySQL and NAS transports has not been qualified against the OpenSSF key-length policy. Do not expose these administrative services to an untrusted network or claim that a private address itself encrypts traffic. A complete secure deployment profile remains work in progress; an offline archive/restore test is not a substitute for that assessment.
