# PR Agent as a reusable workflow

- **Status**: valid
- **Authors**: Enrique González Paredes (@egparedes)
- **Created**: 2026-11-14

Why-statement: In the context of GT4Py and icon4py each running their own copy of a PR Agent workflow (`pull_request_target` and comment commands, an inference key, a write token), facing copies that had already drifted (icon4py's review round added a command allowlist, retry and timeout rules and a failure comment that GT4Py lacks), we decided to publish it as a second reusable workflow in this repository, split into a keyless `authorize` job, a keyed `pr-agent` job and a keyless `report` job, with the shell logic as tested scripts fetched from the workflow's own commit, to achieve one reviewed implementation with per-repository policy and keys. We considered keeping the copies and a single-job port, and accept that each caller needs an environment with a dedicated key and an event policy, and that a central change reaches a caller only through a reviewed pin update.

## Context

- The production version is icon4py's `.github/workflows/pr-agent.yml` (C2SM/icon4py PR 1547, reviewed twice, four live runs). Its reviewers asked for the inline shell to become helper scripts; inside one repository that needed a checkout in a job holding the key.
- [ADR 0002](0002-reusable-workflow.md) established the pattern: `workflow_call`, scripts fetched from `job.workflow_repository` at `job.workflow_sha`, the key as an explicitly mapped environment secret, a central endpoint allowlist, SHA pins.
- PR Agent (v0.50.0, the Docker image) reads the diff and the caller's `.pr_agent.toml` through the API; it runs no PR code. Its settings come from the step environment, then the repository's `.pr_agent.toml`, then comment arguments, except host-only keys. It has no setting that restricts commands or arguments.
- icon4py's job used a repository secret shared with another workflow. The agent review requires a dedicated key in an environment restricted to the default branch.

## Decision

- `.github/workflows/pr-agent.yml` (`on: workflow_call`) and `pr-agent/` (scripts `authorize`, `check-command`, `resolve-models`, `report-outcome`, `api-bases.txt`, tests). The behaviour is icon4py's, with the differences listed below.
- Three jobs. `authorize` has no environment and no secret: it validates the inputs, applies the event rules, the command filter and the writer check, and resolves the model chain. `pr-agent` alone declares the caller's environment and receives the key; it runs only the pinned image and a key check, no script and no checkout. `report` has no secret and posts or removes the failure comment, also after a failed `pr-agent` job. So a refused comment or a non-writer never reaches the environment, and records no deployment.
- Inputs replace the repository variables (callers map them from variables), with empty meaning default; `api_base` selects an entry of `pr-agent/api-bases.txt`; `environment` and `docs_url` are static. Models have no default.
- The caller keeps the triggers, the calling job's `permissions:` (`contents: read`, `issues: write`, `pull-requests: write`), `concurrency`, the explicit secret mapping, the environment and key, the event policy, `.pr_agent.toml` and its review rules.
- Differences from icon4py's workflow:
  - the `--` filter over-approximates PR Agent's parser (quotes and backslashes removed, `--` refused unless it follows a letter, digit or `_`), because icon4py's regex let `\--x`, `-\-x` and `"-"-x` through; a differential test runs it against a copy of the parser;
  - `pr_description.publish_labels: "false"` is pinned in the step environment like the review-label settings, so the no-labels invariant does not rest on PR Agent's default;
  - the failure report is a separate job and also covers an environment denial, an image pull failure and a missing key;
  - `pull_request` is accepted as a caller test mode, keyed only if the caller widens its environment's branch rule; `dry_run` and the self-test's `test_pr` run without a key.
- The repository's self-test covers the keyless path end to end (a local call with `test_pr`), the scripts and the workflow's structure, and checks that the pinned image digest exists. PR Agent never runs here, since this repository holds no key; keyed tests run from a caller.

## Consequences

- One implementation; GT4Py gets icon4py's review-round fixes when it switches. GT4Py's `/describe` then posts a comment instead of rewriting the PR body.
- A caller's workflow is about 35 lines; its checklist is in [pr-agent.md](../pr-agent.md#required-settings-in-the-calling-repository).
- Each caller needs a new dedicated key in an environment; icon4py's shared repository secret is not reused.
- The release, pinning and supply-chain consequences of ADR 0002 apply unchanged; PR Agent ships in the same releases as the agent review.
- An image upgrade must re-check the copied parser and the host-only keys.

## Alternatives considered

### Keep a copy in each repository

- Good, because each repository tests changes in place.
- Bad, because the copies had already drifted on security-relevant points, and every fix had to be ported by hand.

### Port it as one job

- Good, because it is closest to the reviewed original and needs one runner.
- Bad, because every comment starting with `/` would enter the environment (a deployment record, and the key in the job) before the command filter runs, and a failure before the PR Agent step (environment denial, image pull) could not be reported on the PR.

### Use `environment.deployment: false`

- Good, because no run would record a deployment.
- Bad, for now: whether the environment's branch rule still applies with it is not documented and not tested here, and that rule is what keeps the key from branch workflows. It can be adopted after a test.

## References

- [pr-agent.md](../pr-agent.md): usage, inputs, adopter checklist, invariants.
- C2SM/icon4py PR 1547 and its `docs/development/pr_agent.md`; GridTools/gt4py `docs/development/tools/pr-agent.md`.
- [PR Agent](https://github.com/The-PR-Agent/pr-agent) v0.50.0: `pr_agent/agent/pr_agent.py` (`_run_command`), `pr_agent/config_security.py`, `pr_agent/servers/github_action_runner.py`.
