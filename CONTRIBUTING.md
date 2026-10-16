# Contributing

Use GitHub issues for reproducible bugs and concrete enhancement proposals, and pull requests for changes. English is the shared project language. Include expected/actual behavior, the source version and a minimal synthetic example. Never upload real Home Assistant backups, credentials or household/device identifiers. Report vulnerabilities privately as described in SECURITY.md.

Contributions are accepted under the project's MIT license, preserving notices for imported upstream material. Follow the Code of Conduct. Discuss changes to backup format, update approval semantics or privileged deployment access before implementation.

Use Python 3.14 and the pinned validation requirements. Run `python tools/validate.py`, `python -m unittest discover -s tests`, and the relevant Helm/Kustomize checks described in README.md. A change to the public file inventory must update publication.json deliberately. New functionality requires positive and negative tests; bug fixes require a regression test. Include archive traversal, symlink/race, authorization, interrupted-operation and credential-disclosure cases when affected. Runtime tests use synthetic Home Assistant state in a network-disabled container.

All maintained source remains in static-analysis scope. CodeQL examines Python and workflow code. Fix applicable security findings; any false-positive or intentional fixture exception needs a specific explanation. Do not silence broad warning classes or remove checks to obtain a green badge.

Use a normal pull request with independent review, successful required CI and resolved discussions. Source acceptance does not authorize cluster changes. Deployment, restore, credential changes, an infrastructure apply and release promotion require the documented operator acceptance process. Record security fixes and relevant CVE/GHSA/Go advisory identifiers in CHANGELOG.md and release notes.
