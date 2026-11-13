# Agent Review

The reusable `Agent Review` workflow ([`.github/workflows/agent-review.yml`](../.github/workflows/agent-review.yml)) posts an advisory review of a pull request written by a coding agent (a _harness_ such as opencode or Pi) that can explore the repository with tools. The agent runs inside the [AWF](https://github.com/github/gh-aw-firewall) sandbox, without network access, and uses the [CSCS inference service](https://docs.cscs.ch/services/inference/) (or another OpenAI-compatible endpoint) through AWF's API proxy. It is designed so that neither the inference key nor a write-capable token is readable by the agent or by code from the PR. It is **never a required status check**: if the agent fails or times out, the run stays green with a warning annotation. It uses no comment commands, so it does not interfere with other review bots such as PR Agent.

An adopting repository adds a small **caller workflow** that calls this one pinned by commit SHA, and keeps its own triggers, environment, key, label and event policy. The design is in [ADR 0001](adr/0001-sandboxed-agent-pr-review.md) (the sandboxed review) and [ADR 0002](adr/0002-reusable-workflow.md) (packaging it as a reusable workflow).

> [!WARNING]
> **Pre-release.** The pipeline was developed and security-reviewed in GridTools/gt4py (draft PR 2985) as an in-repository workflow, and its reusable form was validated in a spike with user-owned repositories. Never exercised end to end: a real `pull_request_target` or `workflow_dispatch` run in an organization repository, a non-writer being denied, a call across organizations (C2SM → GridTools), and the key-isolation canaries with the final AWF flags (they ran in separate configurations in the AWF probe). In the hostile-PR suite the real harnesses did not attempt the injected attacks, so those runs show that the models complied with the prompt, not that the sandbox contained them; containment rests on the canary and offline tests.

## Calling it

`.github/workflows/agent-review.yml` in the adopting repository:

```yaml
name: Agent Review

on:
  # Not 'pull_request': fork PRs get no secrets there. No 'issue_comment'
  # and no 'synchronize'. Automatic review of writers' PRs
  # ('opened', 'reopened', 'ready_for_review') can be added later.
  pull_request_target:
    types: [labeled]
  workflow_dispatch:
    inputs:
      pr:
        description: Pull request number
        required: true
        type: string
      harness:
        description: Harness; 'default' uses the AGENT_REVIEW_HARNESS variable
        type: choice
        default: default
        options: [default, stub, opencode, pi]
      dry_run:
        description: Show the comment in the run summary instead of posting it
        type: boolean
        default: false

permissions: {}

# No cancelling: a run joins its group before the job 'if' is evaluated, so
# an unrelated label would cancel an in-flight review.
concurrency:
  group: agent-review-${{ github.event.pull_request.number || inputs.pr }}
  cancel-in-progress: false

jobs:
  review:
    # Avoids creating runs for unrelated labels (Dependabot labels its PRs).
    if: github.event_name == 'workflow_dispatch' || github.event.label.name == 'agent-review'
    uses: GridTools/ci-workflows/.github/workflows/agent-review.yml@<40-hex sha> # vX.Y.Z
    # The ceiling for every job of the called workflow; exactly these two.
    permissions:
      contents: read
      pull-requests: write
    with:
      harness: ${{ (inputs.harness == '' || inputs.harness == 'default') && vars.AGENT_REVIEW_HARNESS || inputs.harness }}
      model: ${{ vars.AGENT_REVIEW_MODEL }}
      pr: ${{ inputs.pr }}
      dry_run: ${{ inputs.dry_run == true }}
      project_name: MyProject
      rule_files: |
        AGENTS.md
        CODING_GUIDELINES.md
      prompt_addendum: .github/agent-review/prompt-addendum.md
    # Explicit mapping, never 'secrets: inherit' (same-organization only, and
    # it would pass every secret).
    secrets:
      inference_api_key: ${{ secrets.AGENT_REVIEW_INFERENCE_API_KEY }}
```

### Inputs

`workflow_call` has no `choice` type, so every value is validated in the `authorize` job, fail-closed.

| Input | Default | Meaning and validation |
| --- | --- | --- |
| `harness` | **required** | `opencode` (recommended), `pi` or `stub` |
| `model` | `""` | required unless `stub`; `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$`; the only model AWF lets through |
| `pr` | `""` | PR number for `workflow_dispatch`; ignored under `pull_request_target` |
| `dry_run` | `false` | show the comment in the run summary instead of posting it |
| `label` | `agent-review` | the label that requests a review |
| `environment` | `agent-review` | the caller's environment used by the `review` job |
| `project_name` | repository name | used in the prompt |
| `rule_files` | `AGENTS.md` | paths the prompt tells the agent to **follow**, in order; each is reset to the PR's base version by construction |
| `instruction_files` | `""` | extra reset patterns, syntax of [`instruction-files.txt`](../agent-review/instruction-files.txt) |
| `prompt_addendum` | `""` | one Markdown file in the caller repository, read at the caller's workflow commit and added under the rules (at most 8 KiB). Every file it names in backticks (`.md`, `.mdc`, `.txt`, `.toml`, `.yml`, `.yaml`, `.json`, `.jsonc`) must be covered by a reset pattern, or the review fails closed |
| `api_base` | `https://api.inference.cscs.ch/v1` | OpenAI-compatible `https://` endpoint |
| `requests_per_hour` | 100 | upstream requests per run; ceiling 100 |
| `requests_per_minute` | 30 | ceiling 30 |
| `max_turns` | 80 | AWF `maxTurns`; ceiling 80 |
| `harness_timeout_minutes` | 20 | ceiling 20 |
| `upload_audit` | `false` | upload AWF's audit and proxy logs as an artifact (readable by any signed-in user in a public repository) |
| `test_repository` | `""` | self-test of this repository only (see [Testing changes](#testing-changes)); rejected for any other caller |

Callers can lower the caps, never raise them: `authorize` rejects larger values, and the `review` job clamps them to [`awf.yaml`](../agent-review/awf.yaml) again.

**Secret**: `inference_api_key`, optional; without it, a harness that calls a model is skipped with a notice. **Outputs**: `result` (`posted`, `dry-run`, `skipped`, `missing`, `invalid`; empty if not authorized), `comment_url`, `head_sha`.

## Required settings in the calling repository

1. **The `agent-review` environment, restricted to the default branch** (**Settings → Environments → agent-review → Deployment branches and tags → Selected branches**: the default branch only), holding the secret **`AGENT_REVIEW_INFERENCE_API_KEY`**. Only the `review` job uses the environment; its branch rule is evaluated against the caller run's ref, so `pull_request_target` and dispatches from the default branch pass while branch workflows and dispatches of other refs are denied (verified in the spike). Each run records a deployment in the calling repository. **Never** create a repository- or organization-level secret of the same name: the caller's mapping is evaluated per called job, and in the jobs without the environment it would then yield that other secret's value.
2. **A dedicated CSCS key** in that secret, never shared with PR Agent or other tools: created in the [inference UI](https://ui.inference.cscs.ch/login) for this workflow alone, with its own token budget and reset period, and restricted to the configured model. **Its budget is the only hard spending cap** (see [What remains risky](#what-remains-risky)). A CSCS key also works for `/v1/completions`, `/v1/responses` and `/v1/messages`, and signs in to the inference UI.
3. **An Actions event policy** (**Settings → Actions → Policies**, or `POST /repos/{owner}/{repo}/actions/policies`) scoped by workflow path to the **caller's** file, `.github/workflows/agent-review.yml`, allowing **`pull_request_target` and `workflow_dispatch`**. GitHub blocks `pull_request_target` by default in public repositories from **2 December 2026**. The policy matches the caller's file, never this repository's; `workflow_call` need not be listed; `allowed_events` is an exhaustive allowlist (verified in the spike). Add it even if another policy exists in the repository. `enforcement: evaluate` can be used to dry-run it.
4. Variables `AGENT_REVIEW_HARNESS` (`opencode`) and `AGENT_REVIEW_MODEL` (`zai-org/GLM-5.3-Flash`), and the label `agent-review`.
5. Dependabot's `github-actions` ecosystem covering `.github/workflows`: it proposes updates of the pinned SHA after its default 3-day cooldown.
6. If the organization restricts allowed actions or reusable workflows, allow `GridTools/ci-workflows` and the actions it uses (`actions/upload-artifact`, `actions/download-artifact`, `github/gh-aw-firewall`). Every action here is pinned by full SHA, so a caller requiring SHA pinning works; the caller's own `uses:` of this workflow should be SHA-pinned too, although that setting does not enforce it.

Test with **Run workflow** in dry-run mode first, then with the label on a PR.

## Usage

- Anyone with write access can request a review of any PR not opened by a bot, including a fork or draft PR, by adding the label. The **labeller's** permission is checked, not the author's. To review again, remove and re-add the label. Pushes to the PR do not trigger a review.
- **Run workflow** (`workflow_dispatch`) reviews a given PR number, optionally with another harness or as a dry run.
- If the caller enables the automatic types (`opened`, `reopened`, `ready_for_review`), the PR **author's** permission is checked and drafts are skipped. A `triage` user can then close and reopen a writer's PR to start one run per cycle.
- `triage` is not enough to request a review: the permission API reports it as `read`.

Each harness keeps one comment per PR, updated in place by later runs. The comment names the harness, its version, the model and the reviewed commit; pushes after that commit are not reviewed.

## How it works

| Job | Holds | Does |
| --- | --- | --- |
| `authorize` | read token | Validates the inputs, checks permissions (fails closed on API errors), pins the PR head and base sha, and records this workflow's own commit (`job.workflow_repository`@`job.workflow_sha`). Every other job requires its `allowed` output. |
| `prepare` | read token, **no secrets** | Fetches [`agent-review/`](../agent-review/) from this workflow's own commit (checking that its own `job.workflow_sha` is the one recorded), runs the harness's `install.sh`, which downloads the pinned harness without running any of its code, and uploads it as an artifact. |
| `review` | read token; inference key in **one step's** env (caller's environment) | Fetches `agent-review/` as above and the caller's addendum from the caller repository at the caller's `github.workflow_sha`, copies them to `/opt/agent-review` (root-owned, read-only in the sandbox), writes the PR metadata as raw bytes, and runs AWF twice. First without a key and with only `github.com` allowed: [`run-review`](../agent-review/bin/run-review) `--phase fetch` fetches the PR with git, resets agent instruction files and rule files to the base version and renders the prompt; no agent runs. Then with the key and no allowed domain: `run-review --phase review` runs the harness. Afterwards leftover containers are removed and [`collect-result`](../agent-review/bin/collect-result) copies only `review.md` and `meta.json` out. |
| `publish` | write token (`pull-requests`), no model | On a fresh VM, [`publish-comment`](../agent-review/bin/publish-comment) validates and renders the review in a step without a token, then upserts the comment. |

The generic review rules are in [`prompt.md.tmpl`](../agent-review/prompt.md.tmpl); the project-specific part is the caller's `rule_files` (listed in order) and its addendum. Extend the caller's rule files rather than the prompt. The PR title and body are included as delimited untrusted data. The prompt also asks the agent to finish within about 12 minutes, to read narrowly, to confirm each finding against the code and say how, and to keep maintainability remarks to an optional `### Nits` section of at most three lines; the comment has `### Summary`, `### Findings` (at most five), and optionally `### Nits` and `### Questions`.

## Security invariants

Callers use `pull_request_target` because fork PRs get no secrets under `pull_request`. That trigger runs with secrets next to untrusted code, so all PR content is handled inside AWF only. Keep these when changing anything here:

01. **The real key is only in AWF's API-proxy sidecar** and the environment of the `awf` step that runs the agent; the agent sees placeholders. Never inline it into a `run:` script or pass it to another step. It comes from the caller's environment (`agent-review`), only through the caller's explicit mapping and only in the job that declares that environment, never from a repository secret.
02. **No PR-derived byte is executed or parsed on the host.** No `actions/checkout`, no git on the PR tree outside the sandbox. `pr.json` is written as bytes and parsed inside the sandbox. The one exception is AWF's token-usage log, which the API-proxy sidecar writes out of the sandbox's reach; its request paths and model names come from the agent's requests, so the `Report token usage` step reads only its numeric fields. `prepare` never executes harness code either: it runs on the host with sudo, and its token can read and write the caller's Actions cache.
03. **The sandbox writes only to the review workspace** (`filesystem.allowWrite`); the rest of what AWF mounts from the host (the runner workspace, `/tmp`, `~/.config`, `~/.local/bin`, ...) is read-only to it. Exception: `~/.copilot/{logs,session-state}` stay writable, and AWF moves them into host `/tmp` after the run. So **the host is treated as tainted once `awf` returns**: steps after the agent's run use no token, run no git, use no cache action, and run only `/usr/bin` tools with a fixed `PATH`. The only data leaving `review` is at most 64 KiB per file of regular, single-link files named `review.md` and `meta.json`, copied without following symlinks, after every sandbox container has been removed, plus the token counts in the run summary and, with `upload_audit`, AWF's own audit and proxy logs from `$RUNNER_TEMP`.
04. **The agent has no network access** except AWF's API-proxy listener: its sandbox run allows no domain. Only the first, keyless run, which runs no agent, reaches `github.com`. AWF still allows the inference host for its sidecar in the shared egress proxy, so the agent can probably reach it directly too (not tried), without a key.
05. **Write tokens exist only in `publish`**, which runs no model and no PR code, and treats the review as hostile: it must be valid UTF-8; invisible format characters (zero-width, bidirectional controls, variation selectors, tag characters) are dropped; markup that GitHub renders as nothing (HTML comments, processing instructions, declarations, CDATA, link reference definitions) is broken up, so the review cannot hide text that way nor imitate the comment marker; character references are escaped (`&#64;` and `&commat;` would become mentions, `&rlm;` an invisible character); `@mentions` are neutralised with a zero-width joiner; the length is capped. Rendering needs no token. The marker is matched only on the first line of the bot's own comments, never with `--edit-last`, because other tools (such as PR Agent) may post as the same bot.
06. **Harness, prompt and AWF configuration come from this workflow's own commit** (`job.workflow_repository` at `job.workflow_sha`, the commit the caller pinned, fetched as a tarball and checked to be the same in every job), never from the PR or the caller. **The caller's configuration** (inputs and the addendum) comes from the caller's workflow commit: the default branch under `pull_request_target`, the dispatched ref under `workflow_dispatch` (which the environment rule gates). **Instruction files** matching [`instruction-files.txt`](../agent-review/instruction-files.txt) or the caller's `instruction_files`, and every `rule_files` entry, at any depth, are reset to the version in the PR's **base commit** (or removed): that is the branch the PR targets, not necessarily the default one. The PR's versions are kept as data for the review. A file the addendum names must be covered too, or `run-review` fails closed; `tests/test-instruction-coverage.sh` checks this.
07. **The `review` job starts no service on the host.** The sandbox can connect to any host port bound to `0.0.0.0` through AWF's network gateway (`172.30.0.1`); on a stock runner that is only `sshd`. No `services:` containers, no daemons started in a step.
08. **AWF's logs are read only from `$RUNNER_TEMP`** (`--proxy-logs-dir`, `--audit-dir`). Without `--proxy-logs-dir`, AWF moves them to `/tmp` after the run, where the sandbox can plant fake ones.
09. **Only AWF's OpenAI listener is configured**: never an `anthropic` target or `ANTHROPIC_API_KEY`. In AWF v0.29.12 the Anthropic listener's `POST /messages` is not counted by `maxTurns`, and the key breaks the entrypoint under `filesystem.allowWrite`. The run log shows `API proxy enabled: OpenAI=true, Anthropic=false`.
10. **Pinned**: actions by commit SHA, the runner image (`ubuntu-24.04`, which is the sandbox's userland since AWF chroots into the host filesystem), the AWF version, AWF images by digest (the setup action's `image-tag` output, taken from the attested release at run time), and each harness by exact version with an integrity check. Not pinned: the runner image's own updates, and the model behind its id.
11. **The sandbox's output cannot issue workflow commands**: every line of `awf` output is prefixed. Harness diagnostics go through `diag` in `common.sh`, which does the same.
12. **Per-job least privilege under the caller's ceiling**: top-level `permissions: {}`; `authorize` and `review` read contents and pull requests, `prepare` reads contents, `publish` adds `pull-requests: write`. The caller grants exactly `contents: read` and `pull-requests: write`; a job asking for more fails every caller at startup, so a new permission is a breaking change.

## What remains risky

- **Prompt injection can steer the review's content**: a false approval, misleading findings, links. Mitigations: the comment is labelled advisory, never a status, names the reviewed commit and says the PR can steer it; mentions are neutralised and the size is capped. Cross-references such as `owner/repo#123` are not neutralised and can create backlinks in other repositories. The caller's rule files are reset to base (invariant 6), but files they in turn point at (`CONTRIBUTING.md`, the ADRs) are not, so injected text there is still read as context; it can only steer review content. Visible content (links, images, collapsed `<details>`) is not filtered.
- **Budget burn, driven by the content of any reviewed PR.** The agent has a shell and can call AWF's listener directly, so the per-run bound is AWF's rate limiter, not the turn cap: at most **100 upstream requests per run** (`rateLimiting.requestsPerHour`; the run is shorter than an hour), paced at 30 per minute, each up to the model's context and output limits. AWF's `maxTurns` (80) stops a well-behaved harness but is not a hard cap: it counts only `POST`s to chat-completions, responses and messages paths, when the response ends. Other paths are forwarded with the real key and not counted (the key works for `/v1/completions`), and concurrent calls are all admitted before any is counted. AWF forwards every method and path under `/v1` (dot segments cannot leave `/v1`; encoded separators such as `%2f` are passed through), has no `max_tokens` clamp, and skips the model allowlist when the body has no string `model` or is not JSON. All of this was checked in GT4Py's fork against a fake upstream: 100 concurrent chat calls all reached it under a turn cap of 20, and 100 calls to uncounted paths reached it under the production limits before the hourly limit answered 429. Hence the dedicated key's budget (Required settings, item 2) is the real cap.
- **A sandbox escape** would expose the key on the host. Beyond kernel and Docker bugs, this includes AWF itself: it runs as root on the host, and its API-proxy sidecar, which holds the key, parses requests from the sandbox; both are young code (the AWF probe worked around three of its bugs). Mitigations: the dedicated, low-budget key, quick to rotate; gVisor (`--container-runtime gvisor`) is planned once the harnesses are known to work with it.
- **Egress**: the keyless fetch run reaches `github.com` and its subdomains, with git only. The agent's run reaches the API proxy, the inference host (without a key) and the runner's `sshd` through AWF's gateway, and runs for up to 25 minutes.
- **Live logs**: the review step's log shows the agent's output (prefixed) while it runs; it carries no secret.
- **Central changes reach every caller that updates its pin.** Callers pin a full SHA and review Dependabot's update PRs; releases flag new permissions, inputs, ceilings, harness pins and AWF versions as breaking.
- `@`, `&` and `]:` are neutralised everywhere, including code blocks, so a Python decorator copied from a review contains an invisible character, and character references show literally.

## Configuration notes

**Recommended**: `opencode` with `zai-org/GLM-5.3-Flash`; `pi` with the same model is the alternative (more findings per PR, 1.5–2× the tokens). `zai-org/GLM-5.3` (non-Flash) with opencode is not recommended at the current caps: on real PRs it produced no review, timing out at the 20-minute harness limit. This is from a quality evaluation of dry runs on three merged GT4Py PRs.

Ceilings, changed by a reviewed PR here; callers can only lower the first four ([`awf.yaml`](../agent-review/awf.yaml)): 100 requests per hour and 30 per minute to the API proxy (the per-run bound), `apiProxy.maxTurns` 80 (stops a well-behaved harness; opencode also stops itself after 35 steps, Pi has no cap of its own), 20 minutes for the harness, 25 for AWF's `agentTimeout`, 30 for the review step and 45 for the job. AWF's AI-credit cap is not used, since it prices CSCS models with an estimate, after the response.

AWF's runtime model catalogue is disabled (`apiProxy.modelCatalogue.enabled: false`): otherwise its API-proxy sidecar makes ~7 GETs per run to `api.github.com` (`github/gh-aw-firewall`'s pricing snapshot). Those carry no data about the run and no token, but the AI-credit accounting they feed is unused here. Note that the inference host (added via `baseUrlEnv`) is marked _sensitive_ by AWF and is therefore excluded from Squid's `access.log` and `audit.jsonl`, so model traffic does not appear in the egress logs by design; it is visible only in the API-proxy's own `token-usage.jsonl`.

The `stub` harness makes no model call and posts a fixed review containing hostile content; it is for testing the pipeline only.

## Harness adapters

A harness is a directory `agent-review/harnesses/<name>/`; the name must also be added to the `case` in the `Authorize` step, and callers may add it to their `workflow_dispatch` choice. The contract is in [`harnesses/README.md`](../agent-review/harnesses/README.md):

- `install.sh <prefix>` runs in `prepare` and installs an exact version, integrity-checked, into `<prefix>`, writing the version to `<prefix>/VERSION`, without executing any harness code. The prefix is unpacked to `/opt/agent-review/harness/<name>` and must run from there without network access.
- `run.sh` runs inside the sandbox, from the PR checkout, with `PROMPT_FILE`, `REPO_DIR`, `OUT_DIR`, `CONTEXT_DIR`, `MODEL`, `HARNESS_PREFIX` and `PR_NUMBER` set, plus AWF's `AWF_API_PROXY_OPENAI_URL`. It checks the harness version, keeps its configuration in a scratch home under `$TMPDIR` (in the workspace), disables updates, telemetry and sharing, and writes `$OUT_DIR/review.md`. Exit codes: 0 review written, 2 no review, anything else a failure; it must return 2, not 0, when the harness exits 0 without a real answer.
- `wire` says whether the harness calls a model: `openai-chat` (through AWF's OpenAI listener) or `none`.
- File names the harness reads as instructions or configuration, at any depth, go into `instruction-files.txt`.

The [adapters README](../agent-review/harnesses/README.md) compares the harnesses (version, wire, how the repository's own agent configuration is disabled, turn cap); each adapter's `README.md` explains its pins, upgrades and isolation. In short: both use the OpenAI chat API on AWF's OpenAI listener, the only one configured (AWF v0.29.12 must not get `ANTHROPIC_API_KEY`: its Anthropic listener's `POST /messages` escapes the turn count, and its entrypoint fails under `filesystem.allowWrite`); opencode and Pi have switches that ignore the repository's agent configuration (which can otherwise execute code from the PR).

## Testing changes

This repository's [`self-test.yml`](../.github/workflows/self-test.yml) runs on every pull request, on `main`, weekly and on demand:

- pre-commit (actionlint, shellcheck, file checks);
- the script tests: `agent-review/tests/test-collect-result.sh`, `test-publish-comment.sh`, `test-instruction-coverage.sh` (offline) and `test-run-review.sh` (fetches a real PR from github.com, no AWF);
- the whole pipeline through a **local** call (`uses: ./.github/workflows/agent-review.yml`), so the files fetched at `job.workflow_sha` are the commit under test, with the `stub` harness, no key and a forced dry run, against a fixed public PR of GridTools/gt4py (`test_repository`); the dry-run comment is in the run summary.

The self-test path (`test_repository`, and the `pull_request`, `push` and `schedule` events) is accepted only when the workflow is called from this repository itself with the `stub` harness; it skips the writer check, since nothing can be spent or posted; the intent is that Dependabot's and outside contributors' PRs can run it too (not yet observed: their read-only token might still fail the permission ceiling at startup).

Before a release, also run a caller in a test repository (cross-repository call, environment rule, a real post on a fixture PR). To try an adapter without a key, run `install.sh <prefix>`, then `run-review` with `AWF_API_PROXY_OPENAI_URL` pointing at a local fake OpenAI server.

## Releases

Semver tags `vX.Y.Z` on reviewed commits of `main`, with release notes that flag **breaking** changes: a new permission in any job, a new required input, a raised ceiling, a new harness pin or AWF version. No moving major tag (`v1`): it would let changes reach callers unreviewed.
