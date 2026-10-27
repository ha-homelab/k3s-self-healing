# Independent review and protected merge

The immutable shared workflows read GitHub metadata only. Neither caller checks
out or executes pull-request source. Approval uses the default-branch
`pull_request_target` definition and requires a ready, same-repository PR to the
default branch, an allowlisted author, and either the `auto-approve` or `automerge` label.

The shared job uses `GITHUB_TOKEN` for Dependabot, the existing independent
`BOT_PAT` for maintainer PRs, and its existing GitHub Actions fallback for the
owned bot's PRs. No credential value is stored here.

Use `automerge` to request both the independent review and a protected squash merge.
Use `auto-approve` alone when only a review is wanted. The single-pass evaluator
checks current source and base, an independent current-head approval, all
reported checks, and the exact `CI gate`. Completion events and a recovery
schedule revisit pending requests. It neither bypasses branch rules nor enables
native auto-merge before validation.

Activation requires a separately reviewed selected-repository membership for
the existing organization `BOT_PAT`, and the Terraform policy requiring one
current independent review plus `CI gate` from GitHub Actions app 15368. A source
merge alone configures neither setting. Verify the complete live ruleset,
including the additional Copilot approval flag being false, and qualify one
normal review/merge cycle. Runtime and release permissions remain unchanged.
