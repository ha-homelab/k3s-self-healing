# Changelog

## 0.1.2

- Add frozen beta/RC source plans and archive receipts while preserving the
  deterministic public-source distribution and companion SHA-256 file.
- Require Linux mutation campaigns for recorder metadata and runtime contracts
  before RC publication, alongside all existing source, privacy and HA tests.
- Upgrade: extract the exact source archive separately and review private site
  configuration before installation. No cluster, restore or update command is
  executed by packaging or release publication.
- Security: retain the explicit publication inventory, imported HA source
  integrity and privacy checks. The reserved example.test email domain is
  recognized as synthetic test data; actual identifiers remain rejected.
