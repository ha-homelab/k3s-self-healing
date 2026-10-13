# Fuzzing release candidates

The reusable `fuzz.yml` workflow runs bounded, instrumented input tests on public
release candidates. It is not enrollment in Google's OSS-Fuzz service. It does
not run on pull requests, pushes, beta releases, scheduled nightlies or stable
promotion. Existing unit tests and fixed regression cases keep their usual
schedule.

## Adoption

Add meaningful targets that call production parsing, validation or transformation
code. Test application invariants as well as crashes: invalid commands must not
be accepted, normalization must be stable, and numeric results must stay within
their documented domain. Do not replace production functions with copies in the
test, or count random calls to a standard JSON parser as application coverage.

Create `.github/fuzz.json`, commit seed inputs and dependency locks, and add the
following to a public, versioned `.release-policy.json`:

```json
"fuzzing": {"toolkit_ref": "FULL_REVIEWED_TOOLKIT_COMMIT_SHA"}
```

Run `python3 scripts/install_release.py /path/to/consumer` from that reviewed
toolkit checkout. Review the generated release pipeline and vendored release
verification code together. Consumers remain pinned; changing this repository
does not silently change existing consumers.

Example manifest:

```json
{
  "schema": 1,
  "language": "go",
  "go-version-file": "go.mod",
  "setup": [
    ["go", "mod", "download"],
    ["go", "test", "-c", "-fuzz=^FuzzParse$", "-o", "/tmp/parse-fuzz.test", "./internal/parser"]
  ],
  "targets": [{
    "name": "parse",
    "engine": "go",
    "command": ["go", "test", "./internal/parser", "-run=^$", "-fuzz=^FuzzParse$"],
    "seconds": 60
  }]
}
```

`language` selects setup for Go, Rust, Python, JavaScript or C/C++ (`c`). C/C++
manifests must select an explicit installed compiler, such as `clang-18`, and
build host-only targets with libFuzzer and address/undefined sanitizers. `setup` contains
reviewed argument arrays for dependency installation and compilation, never shell
fragments. Pin dependencies; for Python use a dedicated `.fuzz-venv` and a
hash-locked requirements file. Rust targets should build with pinned cargo-fuzz
and `nightly-2026-10-12`, then execute the compiled libFuzzer binary directly.

Each target needs a unique lowercase kebab-case name, `engine` (`go`, `atheris`
or `libfuzzer`), argument-array `command`, and `seconds` (1–120). Optional `cwd`
is relative to the repository. Optional `corpus` names a committed seed directory;
the runner copies it before mutation. Go uses its native `testdata/fuzz` corpus.
The total mutation budget is at most 600 seconds across at most 12 targets.
`go-version-file` is optional, Go-only and relative to the repository.

Runtime flags are supplied centrally. Do not supply time, worker, crash-output or
memory flags in target commands. Build steps must not run random campaigns; use
compile-only commands or fixed seed replay. Keep setup separate from mutation so
dependency access cannot accidentally enable access to production services.

## Execution and cost boundary

Both the generated caller and reusable workflow check public repository metadata,
`workflow_dispatch`, `channel=rc` and the repository default branch before
allocating a fuzz runner. The Python runner repeats the event checks and binds
the checkout to the run's source SHA. A missing/unknown visibility fails closed.
The workflow uses an ephemeral standard Ubuntu hosted runner, read-only repository
permission, no inherited secrets and no persisted checkout credentials.

Dependency builds may access the network. Each target executes with the runner's
unprivileged UID/GID in a new Linux network namespace, with no access to a real
broker, Cerbo or actuator. Only toolchain environment variables are forwarded.
This is network isolation, not a sandbox for malicious source code: adoption
still requires review of the source, dependencies and commands.

Go uses two fuzz workers and a 1536 MiB runtime memory target; libFuzzer/Atheris
uses a 2048 MiB RSS limit and a 10-second per-input timeout. Go's memory target is
not an operating-system hard limit. Each command has a wall-clock timeout,
including a bounded compile/minimization allowance; the entire job is capped at
30 minutes. Successful exit without positive engine execution statistics fails
qualification, including Go's "no fuzz tests to fuzz" result.

## Evidence, failures and stable promotion

Every attempt saves `fuzz-diagnostics-<attempt>` for 30 days with setup logs,
target logs, copied/mutated seed corpora and failure inputs. The successful
`fuzz-report.json` records repository, source SHA, run/attempt, manifest hash,
target inventory, budgets and outcomes. `release-assets-fuzz` retains it for 90
days and the candidate publisher includes it in the release asset inventory.
Projects with signed releases include the report in their signed inventory.
The report is covered by `release-inputs-fuzz.json`, which binds it to the same
frozen version plan and declared source inputs as the packages. This is recorded
separately in `validation_receipts`; it is not a platform build that must be
repeated during a final-package rebuild. Adopting fuzzing requires a new RC:
older candidates without fuzz evidence can no longer qualify for promotion.

Missing, failed, cancelled or unexpectedly skipped RC fuzz checks block the
release gate. Signing also waits for successful RC fuzzing. The publisher checks
the report against the actual source manifest and attempt before publication;
stable verification checks the original RC job and policy. Normal byte promotion
does not rerun mutation. Projects already requiring final package rebuilds retain
their separate final-build acceptance rules; fuzzing does not waive those rules.

On failure, download diagnostics, reproduce the smallest input against the same
source, fix the production bug and commit a regression seed/test. Publish a new
RC from the fixed source. If retrying an infrastructure failure, rerun all jobs
so the report and release evidence refer to one coherent attempt.

README badges should state the actual scope, for example:

```markdown
[![Fuzzing: RC only](https://img.shields.io/badge/fuzzing-RC_only-blue)](docs/fuzzing.md)
```

This is a capability badge, not a passing-status badge. Link the repository's
documentation to its real release runs and reports. Never use an "OSS-Fuzz
integrated" or passing badge without corresponding enrollment or test evidence.
