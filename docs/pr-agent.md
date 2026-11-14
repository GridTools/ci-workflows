# PR Agent

The reusable `PR Agent` workflow ([`.github/workflows/pr-agent.yml`](../.github/workflows/pr-agent.yml)) runs [PR Agent](https://github.com/The-PR-Agent/pr-agent) on an OpenAI-compatible inference endpoint (today only the [CSCS inference service](https://docs.cscs.ch/services/inference/), the one entry of [`api-bases.txt`](../pr-agent/api-bases.txt)) to post an advisory review on pull requests. PR Agent reads the diff through the GitHub API and makes one model call per tool, so it never sees the rest of the repository; for a review by an agent that explores the repository, see [Agent Review](agent-review.md). It is **never a required status check**: if PR Agent fails or times out, the run stays green, and a comment on the PR links to the run. A later successful run removes that comment.

An adopting repository adds a small **caller workflow** that calls this one pinned by commit SHA, and keeps its own triggers, environment, key, event policy, `.pr_agent.toml` and review rules. The design is in [ADR 0003](adr/0003-pr-agent-reusable-workflow.md).

> [!WARNING]
> **Pre-release.** This workflow has run only through this repository's self-test (a local call in dry run, which never starts the keyed job), plus the caller tests recorded in the PR that added it. It ships in `v0.1.0` together with the agent review, after the [release checklist](#release-checklist). Not observed yet: what a deployment of the `pr-agent` job shows on a PR timeline under `pull_request_target` and `issue_comment`; a call from another organization under those events.

## Calling it

`.github/workflows/pr-agent.yml` in the adopting repository:

```yaml
name: PR Agent

on:
  # Not 'pull_request': fork PRs get no secrets there.
  pull_request_target:
    types: [opened, reopened, ready_for_review]
  issue_comment:
    types: [created]

permissions: {}

# Never cancel: runs join the group before the job 'if' is evaluated, so any
# PR comment would cancel an in-flight review.
concurrency:
  group: pr-agent-${{ github.event_name }}-${{ github.event.comment.id || github.event.pull_request.number }}
  cancel-in-progress: false

jobs:
  pr-agent:
    uses: GridTools/ci-workflows/.github/workflows/pr-agent.yml@<40-hex sha> # vX.Y.Z
    # The ceiling for every job of the called workflow; exactly these three.
    permissions:
      contents: read
      issues: write
      pull-requests: write
    with:
      model: ${{ vars.PR_AGENT_MODEL }}
      fallback_models: ${{ vars.PR_AGENT_FALLBACK_MODELS }}
      token_budget: ${{ vars.PR_AGENT_TOKEN_BUDGET }}
      max_output_tokens: ${{ vars.PR_AGENT_MAX_OUTPUT_TOKENS }}
      ai_timeout: ${{ vars.PR_AGENT_AI_TIMEOUT }}
      docs_url: https://github.com/<owner>/<repo>/blob/main/docs/pr_agent.md
    # Explicit mapping, never 'secrets: inherit'.
    secrets:
      inference_api_key: ${{ secrets.PR_AGENT_INFERENCE_API_KEY }}
```

The structural filters (bots, drafts, comments that do not start with `/` or are not on a PR) are in the called workflow's first job, so the caller needs no `if:`.

### Inputs

Every value is validated, fail-closed, in the keyless `authorize` job by [`bin/authorize`](../pr-agent/bin/authorize) and [`bin/resolve-models`](../pr-agent/bin/resolve-models), with offline unit tests in [`pr-agent/tests/`](../pr-agent/tests/). An **empty** value means the default, so an unset repository variable can be mapped directly.

| Input | Default | Meaning and validation |
| --- | --- | --- |
| `model` | **required** | model id as the endpoint lists it (`GET /v1/models`), without the `openai/` prefix, which the workflow adds; `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$` |
| `fallback_models` | **required** | comma-separated ids, same syntax, tried in order; `none` for no fallback |
| `token_budget` | `262144` | context window PR Agent assumes; a positive integer without leading zeros, greater than `max_output_tokens` |
| `max_output_tokens` | `100000` | tokens reserved for the answer, reasoning included |
| `ai_timeout` | `720` | seconds one model may take before the next one is tried; the whole chain must fit the job limit (see [Configuration notes](#configuration-notes)) |
| `api_base` | `https://api.inference.cscs.ch/v1` | where the key is sent: exactly one of the URLs in [`api-bases.txt`](../pr-agent/api-bases.txt), byte for byte; empty means the first. Omit it |
| `environment` | `pr-agent` | the caller's environment holding the key, used only by the `pr-agent` job |
| `docs_url` | `""` | the caller's own PR Agent page, linked from errors and the failure comment; `https://` and only `[A-Za-z0-9._~/#%-]` |
| `dry_run` | `false` | run `authorize` and `Resolve models` only: no key, no environment, nothing posted |
| `test_pr` | `""` | self-test of this repository only (`owner/repo#number`); rejected for any other caller |

**Secret**: `inference_api_key`. It must be an **environment** secret: `authorize`, which has no environment, fails if the mapped name resolves there. **Output**: `result`: `success` or `failure` (the PR Agent step), `dry-run`, `skipped` (not authorized, or not an event for PR Agent), `incomplete` (the `pr-agent` job did not finish, for example an environment denial), or empty on an error in `authorize`.

**Which inputs may come from repository variables**: `model`, `fallback_models` and the three limits, so they can be changed without a PR; they are validated and the key's own restrictions bound them. **Never** map `environment`, `api_base` or `docs_url` from a variable, a dispatch input or the event: whoever can change a variable needs no review.

## Required settings in the calling repository

1. **The `pr-agent` environment, restricted to the default branch** (**Settings → Environments → pr-agent → Deployment branches and tags → Selected branches**: the default branch only), holding the secret **`PR_AGENT_INFERENCE_API_KEY`**. Create it **before** the caller workflow is merged: GitHub creates an unknown environment on first use, unprotected. Both events run on the default branch, so they pass the branch rule; branch workflows are denied. Only the `pr-agent` job declares the environment, so refused comments and denied users record no deployment; each run of PR Agent records one. **Never** create a repository- or organization-level secret of the same name (`authorize` fails if the mapped name resolves without the environment), and never pass another environment name for tests. Turn off **Allow administrators to bypass configured protection rules** on the environment.
2. **A dedicated key** in that secret, never shared with another workflow or tool: created in the [CSCS inference UI](https://ui.inference.cscs.ch/login) for PR Agent alone, with its own token budget and reset period, and restricted to the models in the chain. Its budget is the hard spending cap.
3. **An Actions event policy** (**Settings → Actions → Policies**, or `POST /repos/{owner}/{repo}/actions/policies`) scoped by workflow path to the **caller's** file, `.github/workflows/pr-agent.yml`, allowing **`pull_request_target` and `issue_comment`** (and **`pull_request`** while a [caller test](#caller-test-mode) runs). GitHub blocks `pull_request_target` by default in public repositories from **2 December 2026**. `allowed_events` is an exhaustive allowlist, so an event missing there is blocked; `workflow_call` need not be listed.
4. **Repository variables** `PR_AGENT_MODEL` and `PR_AGENT_FALLBACK_MODELS` (required), and optionally `PR_AGENT_TOKEN_BUDGET`, `PR_AGENT_MAX_OUTPUT_TOKENS` and `PR_AGENT_AI_TIMEOUT`.
5. **`.pr_agent.toml`** on the default branch (the review policy) and the review-rules files it lists in `repo_context_files`. PR Agent reads both through the API from the caller repository's default branch; nothing of them is in this repository.
6. **A pin** to a tagged commit of this repository's `main`, checked on every update (see [Pinning and updating](agent-review.md#pinning-and-updating); the same commands apply). Dependabot's `github-actions` ecosystem proposes updates.
7. If the organization restricts allowed actions or reusable workflows, allow `GridTools/ci-workflows` and the Docker image `pragent/pr-agent`.

Before merging the caller, its plumbing can be tested in [caller test mode](#caller-test-mode). After merging, check the first run: the `authorize` notice names `GridTools/ci-workflows@<sha>`, and the `pr-agent` job's log shows `Applying repo settings (sections: [...])`.

## Usage

- `/review` runs automatically when a non-draft PR is opened, reopened or marked ready, if its **author** has write access. Bot-authored PRs and PRs matching PR Agent's default `ignore_pr_title` (`^\[Auto\]`) are skipped.
- Users with write access can comment `/review`, `/improve`, `/ask "<question>"`, `/describe` or `/help` on any PR, including a draft or a fork's. The **commenter's** permission is checked. `/describe` posts its summary as a comment and never edits the PR description.
- Other commands (`/generate_labels`, `/update_changelog`, ...) and any `--section.key=value` argument are refused with a notice in the run: they could write labels or override the settings below. Since PR Agent strips quotes and backslashes, `--` is refused anywhere it does not directly follow a letter, digit or `_` (`a--b` in a question is fine; `"--x"` is not).
- `triage` is not enough: the permission API reports it as `read`. `maintain` reports as `write`.

## How it works

| Job | Holds | Does |
| --- | --- | --- |
| `authorize` | read token, **no secrets**, no environment | Fetches [`pr-agent/`](../pr-agent/) from this workflow's own commit (`job.workflow_repository`@`job.workflow_sha`), runs [`bin/authorize`](../pr-agent/bin/authorize) (inputs, event rules, [`bin/check-command`](../pr-agent/bin/check-command), the writer check, fail-closed on API errors) and [`bin/resolve-models`](../pr-agent/bin/resolve-models) (model chain, limits, step timeout). |
| `pr-agent` | the key (caller's environment), write token for issues and PRs | Only if authorized and not a dry run. Checks that the key arrived, then runs the image pinned by digest with `continue-on-error` and the derived step timeout. No checkout and no script of ours. |
| `report` | write token, **no secrets** | After `pr-agent`, even if that job failed: [`bin/report-outcome`](../pr-agent/bin/report-outcome) posts or updates one failure comment (marker `<!-- pr-agent-run-failed -->`), deleting older duplicates, or deletes them all after a success. A cancelled run reports nothing. |

The failure comment exists because a run started by a comment attaches its check to the default branch's head, never to the PR, and an automatic run's check is green with only an annotation. Since `report` is a separate job, it also covers failures before PR Agent starts (environment denial, image pull, missing key), which an in-job step could not.

PR Agent's settings come from three places, later ones overriding earlier ones: the workflow's step environment, the caller's `.pr_agent.toml` on the default branch, and (blocked here) comment arguments. Host-only keys (`openai.key`, `openai.api_base`, `config.num_retries`, `pr_reviewer.publish_error_details`, ...) are accepted only from the environment.

## Security invariants

Callers use `pull_request_target` because fork PRs get no secrets under `pull_request`. Keep these when changing anything here:

1. **Never check out or run PR code** (no `actions/checkout`, no script in the `pr-agent` job), and never grant `contents: write`. PR Agent reads the diff through the API.
2. **`authorize` is the only gate**: `pull_request_target` ignores the fork-approval setting. It requires write access from the PR author (automatic runs) or the commenter (commands) and fails closed. `author_association` is not used: it hides private organization members and does not imply write access.
3. **Model output never writes labels or the PR description**: review labels and `/describe` labels are off, `/describe` posts a comment, and `authorize` refuses the commands and arguments that could change either (PR Agent has no setting that restricts commands or arguments). These settings are in the step environment, so a failed fetch of `.pr_agent.toml` cannot re-enable them; a reviewed change to the caller's `.pr_agent.toml` still can. [`test-command-filter.py`](../pr-agent/tests/test-command-filter.py) checks the filter against a copy of PR Agent's comment parser.
4. **The key is only in the `pr-agent` job**, which alone declares the caller's environment, only in the PR Agent step and a boolean check, and goes only to an endpoint of [`api-bases.txt`](../pr-agent/api-bases.txt) in the pinned commit. [`test-workflow-structure.py`](../pr-agent/tests/test-workflow-structure.py) checks this.
5. **The image is pinned by digest**, since it receives the key; the self-test checks that the digest exists and whether its tag has moved.
6. **The scripts come from this workflow's own commit**, fetched as a tarball and checked to be the same commit in every job; never from the caller or the PR.
7. **Model ids are validated**: PR Agent's settings loader (dynaconf) interprets values, and a leading `@` would be a directive.

## What remains risky

- **Prompt injection can steer the review's content**: a PR's diff, title and description are model input. The review is advisory, never a status, and writes no labels or description.
- **Budget burn**: every writer's PR and every command spends the key's budget. A diff over the token budget is sent in up to three chunks, each billed. The dedicated key's budget is the cap.
- **Writers can steer settings through `.pr_agent.toml`**, by a reviewed PR to the default branch; the settings in the step environment are defaults for it, not ceilings.
- **Central changes reach every caller that updates its pin**, as for the agent review ([What remains risky](agent-review.md#what-remains-risky)).
- **Caller test mode with the key** (below) runs the caller's workflow file from the PR branch; the environment's branch rule is what keeps that from happening outside a deliberate test.

## Configuration notes

- Model ids are stored without the `openai/` prefix, which selects LiteLLM's OpenAI-compatible route. CSCS retires models on short notice; check them against the [model table](https://docs.cscs.ch/services/inference/api/).
- The token limits apply to every model in the chain, so they must fit the smallest one: the output reserve is sent as `max_tokens`, which a model with a lower output limit rejects. Too small an output reserve makes reasoning models return empty responses (`finish_reason: length`; 32768 was too small for GLM models). Check a fallback before adding it, since it is only reached when the models before it fail.
- The step limit is `ceil(chain × ai_timeout / 60) + 2` minutes, so a model that times out is not retried: the next one is tried. `Resolve models` rejects a chain whose step limit plus two minutes exceeds the job's 45 minutes, since a job timeout is a cancellation, which `continue-on-error` does not cover. A rejected request (429, 5xx) is retried twice on the same model first (`num_retries`); slow rejections can still use up the step limit, which the failure comment then reports.
- `enable_large_pr_chunking` in `.pr_agent.toml` sends the chunks in parallel, so it does not extend the step time.
- `output_run_details = true` in `.pr_agent.toml` makes the review name the model that answered, so a fallback answer is visible.
- Typical runs take 1.5–3 minutes with `GLM-5.3-Flash` and 13–17k input tokens.

### Reading a run's log

- `Applying repo settings (sections: [...])` means `.pr_agent.toml` was applied; the bare `Applying repo settings` appears in every run.
- A `403` on `GET /user` ("Could not resolve the GitHub user login") is harmless.
- `repo_context_files` are always read from the default branch (the PR's base); missing files are skipped. `PR_AGENT_CONFIG_BRANCH` does not affect them.
- Cross-repository references such as `other-org/other-repo#12` are misread as local issues and log a 404 or a sibling-allowlist warning; harmless.

## Testing changes

This repository's [`self-test.yml`](../.github/workflows/self-test.yml) runs on every pull request, on `main`, weekly and on demand:

- pre-commit (actionlint, shellcheck) and zizmor on the workflows;
- the script tests: [`test-authorize.sh`](../pr-agent/tests/test-authorize.sh) (permission levels, API errors, the event matrix, allowed and refused commands, long comments), [`test-command-filter.py`](../pr-agent/tests/test-command-filter.py) (differential, against PR Agent 0.50.0's parser), [`test-resolve-models.sh`](../pr-agent/tests/test-resolve-models.sh) (every validation and the derived timeout), [`test-report-outcome.sh`](../pr-agent/tests/test-report-outcome.sh) (0, 1 and several comments, success and failure, job results) and the [structure test](../pr-agent/tests/test-workflow-structure.py);
- that the pinned image digest exists, and a warning if its tag has moved;
- the keyless path through a **local** call with `test_pr` (this repository's merged PR 1, forced dry run): `authorize` and `Resolve models` run on the commit under test, `pr-agent` and `report` are skipped, and no deployment is recorded.

PR Agent itself never runs here: this repository holds no key.

### Caller test mode

A caller can test its caller workflow from a PR branch by adding `pull_request` to its triggers (and to its event policy) while testing:

- With `dry_run: true`, no key and no environment are used, so any branch works; it proves the call, the inputs and the permission check.
- Without `dry_run`, PR Agent runs for real under `pull_request` (same actions as `pull_request_target`), but only if the environment's branch rule is temporarily widened to the test branch; fork PRs are skipped. A notice says that the caller's workflow file and inputs are PR content in this mode. Revert the trigger, the policy and the branch rule afterwards.
- Comment commands always run the caller workflow from the default branch, so they can only be tested after merging.

## Upgrading PR Agent

Change the digest and the tag comment together, after checking the new release's notes. Then re-check against the new source: the command parser that [`test-command-filter.py`](../pr-agent/tests/test-command-filter.py) copies, the host-only keys (`pr_agent/config_security.py`), and the settings in the step environment. Release it as a change that callers review.

## Releases

Released with the agent review: semver tags on reviewed commits of `main`, no moving major tag; see [Releases](agent-review.md#releases). A new permission, a new required input or a new endpoint in `api-bases.txt` is breaking; a new image is flagged in the notes.

### Release checklist

In addition to the agent review's [release checklist](agent-review.md#release-checklist), before `v0.1.0`:

1. A caller test with the key (`pull_request`, environment widened to the test branch) on a fixture PR: a posted review, the `Applying repo settings (sections: [...])` log line, `Model 'openai/...'` in `authorize`.
2. A forced failure (`ai_timeout: "1"`): the failure comment is posted; a following successful run removes it.
3. A non-writer's PR and a refused command (`/generate_labels`): skipped with a notice, no `pr-agent` job, no deployment.
4. After a caller switches over, one `pull_request_target` run and one `/review` comment from the default branch; record what the deployment shows on the PR.
