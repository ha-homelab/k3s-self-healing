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
