# Agent review as a reusable workflow in a central repository

- **Status**: valid
- **Authors**: Enrique González Paredes (@egparedes)
- **Created**: 2026-11-13
- **Updated**: 2026-11-13 (security review of PR 1: keyed/keyless split, endpoint allowlist, caller test mode, release integrity)

Why-statement: In the context of the sandboxed agent PR review ([ADR 0001](0001-sandboxed-agent-pr-review.md)), facing the need to use it in several repositories of different organizations (GT4Py in GridTools, icon4py in C2SM) without copying a security-reviewed, 700-line workflow and its scripts into each, we decided to publish it as a reusable workflow (`on: workflow_call`) in the public repository GridTools/ci-workflows, called by a small workflow in each repository pinned by commit SHA, to achieve one reviewed implementation with per-repository configuration and keys. We considered keeping a copy per repository and a composite action, and accept that every caller must configure its own environment, key, label and event policy, and that a central change reaches a caller only through a reviewed pin update.

## Context

- The pipeline's trust boundaries are separate jobs with different permissions, an environment holding the key in one job, and per-step token handling. A composite action runs inside one caller job, so it cannot provide them.
- A reusable workflow runs with the **caller's** event, token, environments and `vars`; the caller's `permissions:` are a ceiling checked at startup for every called job (including skipped ones).
- The called workflow must read its own scripts from its own commit, never from the caller or the PR. GitHub exposes that commit as `job.workflow_repository` and `job.workflow_sha` (documented; not available on GHES).
- Environment secrets reach a called job only through the caller's explicit `secrets:` mapping and only in a job that declares the environment; `secrets: inherit` works only within one organization or enterprise.
- Actions event policies (needed for `pull_request_target` from December 2026) match the caller's workflow file.
- These behaviours were verified in a spike with two user-owned public repositories (stub harness, dummy secrets); cross-organization calls were not testable there.

## Decision

- GridTools/ci-workflows holds `.github/workflows/agent-review.yml` (`on: workflow_call`) and `agent-review/` (driver, harness adapters and pins, AWF configuration, generic prompt, tests). It is public, so every caller's token can fetch it.
- Every job fetches `agent-review/` as a tarball of `job.workflow_repository` at `job.workflow_sha`; `authorize` records that commit and every later job checks that its own `job.workflow_sha` matches.
- Repository-specific configuration is passed as validated inputs (`rule_files`, `instruction_files`, `project_name`, caps that can only be lowered) plus one optional prompt addendum file read from the caller repository at the caller's `github.workflow_sha`. Every rule file is reset to the PR's base version by construction; a file the addendum names must be covered by a reset pattern, or the review fails closed.
- The key is a declared `workflow_call` secret, `inference_api_key`, which callers map explicitly from their own environment secret. Only the keyed `review` job declares the caller's environment (named by an input) and references the secret; harnesses that call no model run the same steps (YAML aliases) in a `review-keyless` job without environment or secret. `authorize` decides which, from its harness allowlist, never from an input; keyed runs are accepted only under `pull_request_target` and `workflow_dispatch`. `authorize` fails if the secret resolves in a job without environment (a shared secret).
- The key goes only to an endpoint listed in `agent-review/api-bases.txt` in the pinned commit (today only the CSCS URL); the `api_base` input selects an entry by exact match. A new endpoint is a reviewed, breaking release.
- The caller keeps the triggers, the calling job's `permissions:` (`contents: read`, `pull-requests: write`), `concurrency`, and as settings the environment (default branch only), the dedicated key, the variables, the label and the event policy.
- The repository's own CI calls the workflow locally (`uses: ./...`) with the stub harness against a fixed public PR, in dry-run. This self-test path (another repository's PR, `push`, `schedule`) is accepted only with the stub harness when `job.workflow_repository` equals the calling repository. Any caller may use `pull_request` with the stub (caller test mode, dry run forced by the event name); its configuration is then PR content, so the mode proves plumbing only.
- Releases are semver tags on reviewed commits of `main`, starting with `v0.1.0`, after a cross-repository release checklist; callers pin the full SHA with the tag as a comment, update through Dependabot, and check that each new pin is on `main` (a commit pushed only to a fork resolves through this repository's name). A new permission, a new required input, a raised ceiling, a new endpoint, a harness pin or an AWF version change is released as breaking. No moving major tag.

## Consequences

- One security-reviewed implementation; fixes reach every caller through a reviewed pin update.
- A caller's workflow is about 50 lines; its adopter checklist is in [agent-review.md](../agent-review.md#required-settings-in-the-calling-repository).
- The repository is now part of every caller's supply chain: its `main` is protected (pull request with an approval, linear history, no force pushes or deletions) and it requires SHA-pinned actions. That protection does not cover what callers pin: tags and releases need their own ruleset and immutability, the self-test must be a required status check, and a commit that exists only in a fork resolves through this repository's name, so callers must check that a pin is on `main`. A check inside the workflow cannot replace the caller's check, because the pinned commit supplies the workflow file. Its owners should be more than one person before the first release.
- Adding a permission to any job breaks every caller at startup; this is deliberate.
- Organizations with an allowlist of actions or reusable workflows must allow this repository. Cross-organization behaviour (C2SM → GridTools) and GitHub's workflow lockfile protection are not verified yet.
- `job.workflow_sha` is not available on GitHub Enterprise Server; this repository targets github.com only.

## Alternatives considered

### A copy in each repository

- Good, because each repository controls its copy and can test it in place.
- Bad, because security fixes must be ported by hand, and copies drift.

### A composite action

- Good, because it is easy to call and version.
- Bad, because it runs within one job of the caller: it cannot separate the key, the write token and the untrusted workspace into different jobs, which the design depends on.

### `$/` self-repository references instead of the tarball fetch

- Good, because it removes an API call and puts the central tree on disk.
- Bad, because it is a July 2026 runner feature (≥ 2.336.0); the tarball fetch keyed on `job.workflow_sha` is explicit, logged and the same in every job. It can replace the fetch later.

## References

- [agent-review.md](../agent-review.md): usage, inputs, adopter checklist, invariants.
- GitHub docs: [Reusing workflows](https://docs.github.com/actions/sharing-automations/reusing-workflows), [Contexts: `job`](https://docs.github.com/actions/reference/workflows-and-actions/contexts#job-context).
