# Release read recovery and rerun validation

This project carries two focused fixes from the shared release toolkit:

- [Bounded GET recovery](https://github.com/victron-venus/venus-os-ci-toolkit/commit/f6b7d4dfbe44158663d6d3da8c3092932d59253a): make at most three total attempts (the initial request and up to two retries) for recognized transport failures and HTTP 500, 502, 503 or 504 responses. Metadata attempts have a 30-second limit; whole-asset and Actions ZIP attempts have a 120-second limit. Failed response bytes are discarded. The [response-header refinement](https://github.com/victron-venus/venus-os-ci-toolkit/commit/0260529c7f1aef8da030e02c4a5ee7e49dab181f) distinguishes real HTTP denials from status-shaped payload text without copying the response body. Authentication, certificate, missing-resource and rate-limit errors are not retried; writes remain single attempts.
- [Current-attempt validation](https://github.com/victron-venus/venus-os-ci-toolkit/commit/ae0b6f54b19256c1495c821fb70145eaab6be04e): on a rerun, require successful validation jobs to start at or after the exact attempt start, with ordered job times and executed steps contained within their job. A new job ID or attempt number cannot make a copied earlier success current.

These changes are a partial backport, not a full toolkit upgrade. The existing
policy revision, shared workflow pins, download API, artifact restrictions and
signature requirements remain unchanged. Existing release tags and their
accepted evidence are not rewritten.

## Verify or regenerate

The required release-tooling CI checks discover every
`test_*.py` file in `.github/release-tests`. The project-owned
`test_release_read_safety.py` covers transport denials, write non-replay,
timestamp validation and the actual release gate.
`test_release_response_framing.py` covers partial headers, payload text and
bounded parsing. Keep both files when regenerating release tooling:
restoring the older engine must fail these tests.

```sh
python3 -m unittest discover -s .github/release-tests -p 'test_*.py' -v
python3 scripts/version_plan.py check-base
```

The shared installer writes its known outputs and does not remove these extra
test files. Regeneration from an older toolkit can overwrite the backport, but
the retained tests prevent that regression from passing the required CI gate.
For a repeated partial backport, apply the linked engine fixes, including
the response-header refinement, together. Retain the focused tests and their receipt-fixture updates, and review exact
input/output hashes. A future full toolkit refresh must update its complete
dependency bundle rather than replacing `release_control.py` alone.

After a terminal release infrastructure failure, inspect existing tags, assets
and active runs before retrying. If the release policy permits reuse before
publication starts, use **Re-run all jobs** at the unchanged source. Earlier
attempt results cannot fill missing validation in a later attempt.
