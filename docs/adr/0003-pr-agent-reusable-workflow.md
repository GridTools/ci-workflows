# PR Agent as a reusable workflow

- **Status**: valid
- **Authors**: Enrique González Paredes (@egparedes)
- **Created**: 2026-11-14

Why-statement: In the context of repositories that each run their own copy of a PR Agent workflow (`pull_request_target` and comment commands, an inference key, a write token), facing copies that drift apart on security-relevant points (command restrictions, retry and timeout rules, failure reporting), we decided to publish it as a second reusable workflow in this repository, split into a keyless `authorize` job, a keyed `pr-agent` job and a keyless `report` job, with the shell logic as tested scripts fetched from the workflow's own commit, to achieve one reviewed implementation with per-repository policy and keys. We considered keeping a copy per repository and a single-job workflow, and accept that each caller needs an environment with a dedicated key and an event policy, and that a central change reaches a caller only through a reviewed pin update.

## Context

- [ADR 0002](0002-reusable-workflow.md) established the pattern: `workflow_call`, scripts fetched from `job.workflow_repository` at `job.workflow_sha`, the key as an explicitly mapped environment secret, a central endpoint allowlist, SHA pins.
- PR Agent (v0.50.0, the Docker image) reads the diff and the caller's `.pr_agent.toml` through the API; it runs no PR code. Its settings come from the step environment, then the repository's `.pr_agent.toml`, then comment arguments, except host-only keys. It has no setting that restricts commands or arguments.
- In a single in-repository workflow, the permission and command checks are inline shell in the job that holds the key; moving them to scripts there would need a checkout in that job.
- PR Agent parses a comment with shlex (posix, `"` as the only quote), which removes quotes and backslashes before it recognises `--section.key=value` arguments. A filter that looks for a literal `--` after whitespace or a quote misses `\--x`, `-\-x` and `"-"-x`.

## Decision

- `.github/workflows/pr-agent.yml` (`on: workflow_call`) and `pr-agent/` (scripts `authorize`, `check-command`, `resolve-models`, `report-outcome`, `api-bases.txt`, tests).
- Three jobs. `authorize` has no environment and no secret: it validates the inputs, applies the event rules, the command filter and the writer check, and resolves the model chain. `pr-agent` alone declares the caller's environment and receives the key; it runs only the image, pinned by digest, and a key check, no script and no checkout. `report` has no secret and posts or removes the single failure comment, also after a failed `pr-agent` job (environment denial, image pull, missing key). So a refused comment or a non-writer never reaches the environment and records no deployment.
- The command filter allows only `/review`, `/improve`, `/ask`, `/describe` and `/help`, and over-approximates PR Agent's parser: with quotes and backslashes removed, `--` is refused unless it directly follows a letter, digit or `_`. A differential test runs it against a copy of the parser, and fails when the image changes until the copy is re-checked.
- Model output never writes labels or the PR description: the review-label settings and `pr_description.publish_labels` are off and `/describe` posts a comment, set in the step environment so that a failed fetch of `.pr_agent.toml` cannot change them.
- Inputs carry the model chain and limits (callers map them from repository variables), with empty meaning default; `api_base` selects an entry of `pr-agent/api-bases.txt`; `environment` and `docs_url` are static. Models have no default.
- The caller keeps the triggers, the calling job's `permissions:` (`contents: read`, `issues: write`, `pull-requests: write`), `concurrency`, the explicit secret mapping, the environment and key, the event policy, `.pr_agent.toml` and its review rules.
- `pull_request` is accepted as a caller test mode, keyed only if the caller widens its environment's branch rule; `dry_run` and the self-test's `test_pr` run without a key.
- The repository's self-test covers the keyless path end to end (a local call with `test_pr`), the scripts and the workflow's structure, and checks that the pinned image digest exists. PR Agent never runs here, since this repository holds no key; keyed tests run from a caller.

## Consequences

- One implementation; callers get fixes through a reviewed pin update.
- A caller's workflow is about 35 lines; its checklist is in [pr-agent.md](../pr-agent.md#required-settings-in-the-calling-repository).
- Each caller needs a dedicated key in an environment; a key shared with other workflows cannot be reused.
- Callers lose `/describe` editing the PR description, other commands and comment arguments.
- The release, pinning and supply-chain consequences of ADR 0002 apply unchanged; PR Agent ships in the same releases as the agent review.
- An image upgrade must re-check the copied parser and the host-only keys.

## Alternatives considered

### A copy in each repository

- Good, because each repository tests changes in place.
- Bad, because copies drift on security-relevant points, and every fix must be ported by hand.

### One job

- Good, because it needs one runner and is the simplest form.
- Bad, because every comment starting with `/` would enter the environment (a deployment record, and the key in the job) before the command filter runs, and a failure before the PR Agent step (environment denial, image pull) could not be reported on the PR.

### Use `environment.deployment: false`

- Good, because no run would record a deployment.
- Bad, for now: whether the environment's branch rule still applies with it is not documented and not tested here, and that rule is what keeps the key from branch workflows. It can be adopted after a test.

## References

- [pr-agent.md](../pr-agent.md): usage, inputs, adopter checklist, invariants.
- [PR Agent](https://github.com/The-PR-Agent/pr-agent) v0.50.0: `pr_agent/agent/pr_agent.py` (`_run_command`), `pr_agent/config_security.py`, `pr_agent/servers/github_action_runner.py`.
