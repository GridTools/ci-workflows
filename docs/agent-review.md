# Agent Review

The reusable `Agent Review` workflow ([`.github/workflows/agent-review.yml`](../.github/workflows/agent-review.yml)) posts an advisory review of a pull request written by a coding agent (a _harness_ such as opencode or Pi) that can explore the repository with tools. The agent runs inside the [AWF](https://github.com/github/gh-aw-firewall) sandbox, without network access, and uses the [CSCS inference service](https://docs.cscs.ch/services/inference/) through AWF's API proxy (the only endpoint in the central allowlist, [`api-bases.txt`](../agent-review/api-bases.txt)). It is designed so that neither the inference key nor a write-capable token is readable by the agent or by code from the PR. It is **never a required status check**: if the agent fails or times out, the run stays green with a warning annotation. It uses no comment commands, so it does not interfere with other review bots such as PR Agent.

An adopting repository adds a small **caller workflow** that calls this one pinned by commit SHA, and keeps its own triggers, environment, key, label and event policy. The design is in [ADR 0001](adr/0001-sandboxed-agent-pr-review.md) (the sandboxed review) and [ADR 0002](adr/0002-reusable-workflow.md) (packaging it as a reusable workflow).

> [!NOTE]
> **Status: `v0.1.0`, the first release.** The [release checklist](#release-checklist) was run from a separate test caller: keyed `opencode` and Pi reviews, the label path with real posts, a fork PR by an account without write access carrying hostile content, a git-level fixture, an audit run, and invariant 6 end to end with a file named in the addendum (item 3.11). The run URLs are in the release notes. **Not verified**: the labeller-denial case (it needs an organization repository, where the `triage` role exists); runs in an organization repository, calls from another organization and organization-level settings such as an actions allowlist (the test caller is user-owned); and the key-isolation canaries with the final AWF flags (they ran in separate configurations in the AWF probe). The reset's fail-closed post-conditions did not fire in these runs; the offline tests show them. In the hostile fork PR the harnesses named the injected text and followed none of it, which shows the models' behaviour, not that the sandbox contained an attack; containment rests on the canary and offline tests.

## Calling it

`.github/workflows/agent-review.yml` in the adopting repository:

```yaml
name: Agent Review

on:
  # Not 'pull_request': fork PRs get no secrets there. No 'issue_comment'
  # and no 'synchronize'. Automatic review of writers' PRs
  # ('opened', 'reopened', 'ready_for_review') can be added later, together
  # with the job 'if' (see Usage).
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
      # No 'api_base': the default is the only allowed endpoint. Never map
      # an input from a variable or the event: inputs are trusted config.
    # Explicit mapping, never 'secrets: inherit' (same-organization only, and
    # it would pass every secret).
    secrets:
      inference_api_key: ${{ secrets.AGENT_REVIEW_INFERENCE_API_KEY }}
```

Optionally, a **test job** for the caller's own pull requests (see [Caller test mode](#caller-test-mode)): add `pull_request` to `on:` (for example with `branches: [main]` and `paths: [.github/workflows/agent-review.yml, .github/agent-review/**]`) and a second job:

```yaml
  test:
    if: github.event_name == 'pull_request'
    uses: GridTools/ci-workflows/.github/workflows/agent-review.yml@<same sha> # vX.Y.Z
    permissions:
      contents: read
      pull-requests: write
    with:
      harness: stub # any other harness is rejected under 'pull_request'
      # Not used by the stub. To try that rejection with another harness
      # (checklist 3.2), a model must be given: otherwise the model check
      # fails first ("No model given ...") and the event rule is never reached.
      model: ${{ vars.AGENT_REVIEW_MODEL }}
      pr: ${{ github.event.pull_request.number }}
      project_name: MyProject
      rule_files: |
        AGENTS.md
        CODING_GUIDELINES.md
      prompt_addendum: .github/agent-review/prompt-addendum.md
    # No 'secrets:': the stub runs in the keyless job.
```

### Inputs

`workflow_call` has no `choice` type, so every value is validated in the `authorize` job by [`bin/authorize`](../agent-review/bin/authorize) (Python, [`lib/agent_review/authorize.py`](../agent-review/lib/agent_review/authorize.py)), fail-closed, with offline unit tests ([`tests/test-authorize.sh`](../agent-review/tests/test-authorize.sh), [`tests/python/test_authorize.py`](../agent-review/tests/python/test_authorize.py)). A value must match its whole pattern, in ASCII only (a value that is not UTF-8 fails), and every value quoted in a notice or error is escaped, so it cannot start another workflow command.

| Input | Default | Meaning and validation |
| --- | --- | --- |
| `harness` | **required** | `opencode` (recommended), `pi` or `stub` |
| `model` | `""` | required unless `stub`; `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$`; the only model AWF lets through |
| `pr` | `""` | PR number for `workflow_dispatch` (and `pull_request`, where it defaults to the event's PR); ignored under `pull_request_target` |
| `dry_run` | `false` | show the comment in the run summary instead of posting it; forced under `pull_request`, `push` and `schedule` and with `test_repository` |
| `label` | `agent-review` | the label that requests a review; `^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,49}$` |
| `environment` | `agent-review` | the caller's environment, used only by the keyed `review` job (harnesses `opencode`, `pi`) |
| `project_name` | repository name | used in the prompt |
| `rule_files` | `AGENTS.md` | paths from the repository root the prompt tells the agent to **follow**, in order; each is reset to the PR's base version, which `run-review` checks after the reset; no `..`, no absolute paths |
| `instruction_files` | `""` | extra reset patterns, syntax of [`instruction-files.txt`](../agent-review/instruction-files.txt) (`*` does not cross `/`; a trailing `/` means a directory) |
| `prompt_addendum` | `""` | one path (no `#`, no `..`) of a Markdown file in the caller repository, read at the caller's workflow commit and added under the rules (at most 8 KiB). It must not contain `{{`. Every file it names in backticks with one of the extensions `.md`, `.mdc`, `.txt`, `.toml`, `.yml`, `.yaml`, `.json`, `.jsonc` must be a plain path from the repository root and must be its base version after the reset (see invariant 6); file names without such an extension, or not in backticks, are not checked |
| `api_base` | `https://api.inference.cscs.ch/v1` | where the key is sent: exactly one of the URLs in [`api-bases.txt`](../agent-review/api-bases.txt) (today only the default), compared byte for byte; empty means the default. Omit it |
| `requests_per_hour` | 100 | upstream requests per run; ceiling 100 |
| `requests_per_minute` | 30 | ceiling 30 |
| `max_turns` | 80 | AWF `maxTurns`; ceiling 80 |
| `harness_timeout_minutes` | 20 | ceiling 20 |
| `upload_audit` | `false` | upload AWF's audit and proxy logs as an artifact (readable by any signed-in user in a public repository; see [Troubleshooting](#troubleshooting)) |
| `test_repository` | `""` | self-test of this repository only (see [Testing changes](#testing-changes)); rejected for any other caller |

Callers can lower the caps, never raise them: `authorize` rejects larger values, zero and non-integers (unit-tested), and the review job clamps them to [`awf.yaml`](../agent-review/awf.yaml) again (checked in the self-test's log).

**Secret**: `inference_api_key`, optional; without it, a harness that calls a model is skipped with a notice. It must be an **environment** secret: `authorize`, which has no environment, fails if the mapped name resolves there (a repository or organization secret of that name). **Outputs**: `result` (`posted`, `dry-run`, `skipped`, `missing`, `invalid`; `rendered` if posting the comment failed; empty if not authorized), `comment_url`, `head_sha`.

## Required settings in the calling repository

1. **The `agent-review` environment, restricted to the default branch** (**Settings → Environments → agent-review → Deployment branches and tags → Selected branches**: the default branch only), holding the secret **`AGENT_REVIEW_INFERENCE_API_KEY`**. Only the keyed `review` job (harnesses `opencode`, `pi`) uses the environment; its branch rule is evaluated against the caller run's ref, so `pull_request_target` and dispatches from the default branch pass while branch workflows and dispatches of other refs are denied (verified). Each keyed run records a deployment in the calling repository; `stub` runs use the keyless job, request no environment and record none. A keyed attempt that the environment refuses (a dispatch of another ref, say) still records a deployment, with status `failure`. The deployment's ref is the **PR's head branch** for a label-path (`pull_request_target`) run, which shows as a "deployed" entry on the PR's timeline, and the **dispatched branch** (the default branch, for a keyed run) for a `workflow_dispatch` run. **Never** create a repository- or organization-level secret of the same name: the caller's mapping is evaluated per called job, and in the jobs without the environment it would then yield that other secret's value; `authorize` fails if it does. Turn off **Allow administrators to bypass configured protection rules** on the environment if it is on (`can_admins_bypass`); whether that bypass covers the branch rule is untested, and turning it off costs nothing. **Never** pass another environment name for tests: GitHub creates an unknown environment on first use, unprotected.
2. **A dedicated CSCS key** in that secret, never shared with PR Agent or other tools: created in the [inference UI](https://ui.inference.cscs.ch/login) for this workflow alone, with its own token budget and reset period, and restricted to the configured model. **Its budget is the only hard spending cap** (see [What remains risky](#what-remains-risky)). A CSCS key also works for `/v1/completions`, `/v1/responses` and `/v1/messages`, and signs in to the inference UI.
3. **An Actions event policy** (**Settings → Actions → Policies**, or `POST /repos/{owner}/{repo}/actions/policies`) scoped by workflow path to the **caller's** file, `.github/workflows/agent-review.yml`, allowing **`pull_request_target` and `workflow_dispatch`** (and **`pull_request`** if the caller has the test job of [Caller test mode](#caller-test-mode)). GitHub blocks `pull_request_target` by default in public repositories from **2 December 2026**. The policy matches the caller's file, never this repository's; `workflow_call` need not be listed; `allowed_events` is an exhaustive allowlist (verified). Add it even if another policy exists in the repository. `enforcement: evaluate` can be used to dry-run it.
4. Variables `AGENT_REVIEW_HARNESS` (`opencode`) and `AGENT_REVIEW_MODEL` (`zai-org/GLM-5.3-Flash`), and the label `agent-review`. Map no other input from a variable (see [What remains risky](#what-remains-risky)).
5. A pin to a tagged commit of this repository's `main`, checked on every update ([Pinning and updating](#pinning-and-updating)). Dependabot's `github-actions` ecosystem covering `.github/workflows` proposes updates of the pinned SHA after its default 3-day cooldown; check its PRs the same way.
6. If the organization restricts allowed actions or reusable workflows, allow `GridTools/ci-workflows` and the actions it uses (`actions/upload-artifact`, `actions/download-artifact`, `github/gh-aw-firewall`). Every action here is pinned by full SHA, so a caller requiring SHA pinning works; the caller's own `uses:` of this workflow should be SHA-pinned too, although that setting does not enforce it.

Test with **Run workflow** in dry-run mode first (`stub`, then the real harness), then with the label on a PR. Before merging the caller, its plumbing can be tested in [Caller test mode](#caller-test-mode).

### Pinning and updating

Pin only a commit that is on this repository's `main` and carries a release tag; write the tag as a comment. A commit pushed only to a fork of this repository resolves through this repository's name in the REST API (commits, tarball). In one test (from a fork with Actions disabled), Actions refused such a commit as a reusable-workflow pin ("workflow was not found"), but that behaviour may change, so the caller-side check stays required as defence in depth. A tag can point at an unreviewed side branch, and it can resolve to a different commit even between attempts (re-runs) of the same run, so **pin full SHAs** and check each new pin before merging the update:

```bash
gh api repos/GridTools/ci-workflows/compare/main...<sha> --jq .status   # must print 'identical' or 'behind'
gh api repos/GridTools/ci-workflows/git/ref/tags/<tag> --jq .object.sha  # must be <sha> (annotated tags: dereference)
```

or run [zizmor](https://docs.zizmor.sh/) with its online `impostor-commit` and `unpinned-uses` audits on the caller's workflows. The workflow cannot check this itself: the pinned commit supplies the workflow file that would do the checking. A pull request head of this repository is never a valid pin: merges are squash or rebase, so that commit never reaches `main`.

## Usage

- Only these `pull_request_target` actions start a review: `labeled` (with the configured label), `opened`, `reopened` and `ready_for_review`; any other action the caller's `types` lets through ends `authorize` with a notice. The trigger's `types` should still list only the ones wanted.
- Anyone with write access can request a review of any PR not opened by a bot, including a fork or draft PR, by adding the label. The **labeller's** permission is checked, not the author's. To review again, remove and re-add the label. Pushes to the PR do not trigger a review.
- **Run workflow** (`workflow_dispatch`) reviews a given PR number, optionally with another harness or as a dry run. A keyed harness gets the key only when dispatched from the default branch (environment rule). The `stub` uses no environment, so a writer can dispatch it from any branch: it has no key and posts at most its fixed test review.
- If the caller enables the automatic types (`opened`, `reopened`, `ready_for_review`), the PR **author's** permission is checked. A `triage` user can then close and reopen a writer's PR to start one run per cycle. The documented job `if` admits only dispatches and the label, so extend it together with the trigger's `types`, or those runs are skipped at the caller:

  ```yaml
  jobs:
    review:
      if: >-
        github.event_name == 'workflow_dispatch'
        || (github.event_name == 'pull_request_target'
            && (github.event.action != 'labeled'
                || github.event.label.name == 'agent-review'))
  ```

  Drafts are skipped on the automatic types with no notice (the filter in the first job: the run shows the job as skipped); `ready_for_review` starts the review once the PR is marked ready. A draft can still be reviewed by adding the label.
- `triage` is not enough to request a review: the permission API reports it as `read`.

Each harness keeps one comment per PR, updated in place by later runs. The comment names the harness, its version, the model and the reviewed commit; pushes after that commit are not reviewed.

## How it works

| Job | Holds | Does |
| --- | --- | --- |
| `authorize` | read token, **no secrets** | Records this workflow's own commit (`job.workflow_repository`@`job.workflow_sha`), fetches `agent-review/` from it, and runs [`bin/authorize`](../agent-review/bin/authorize): validates the inputs, picks the endpoint from the allowlist, decides from its harness allowlist whether the run is **keyed**, applies the event rules, checks permissions (fails closed on API errors) and pins the PR head and base sha. Every other job requires its `allowed` output. |
| `prepare` | read token, **no secrets** | Fetches [`agent-review/`](../agent-review/) from this workflow's own commit (checking that its own `job.workflow_sha` is the one recorded), runs the harness's `install.sh`, which downloads the pinned harness without running any of its code, and uploads it as an artifact. |
| `review` (keyed) or `review-keyless` | read token; for `review` only: inference key in **one step's** env (caller's environment) | The two jobs run the same steps (YAML aliases); only `review` declares the environment and references the secret, and only keyed harnesses under `pull_request_target` or `workflow_dispatch` run it, one at a time per PR (its own concurrency group). Fetches `agent-review/` as above and the caller's addendum from the caller repository at the caller's `github.workflow_sha`, copies them to `/opt/agent-review` (root-owned, read-only in the sandbox), writes the PR metadata as raw bytes, and runs AWF twice. First without a key and with only `github.com` allowed: [`run-review`](../agent-review/bin/run-review) `--phase fetch` fetches the PR with git, resets agent instruction files and rule files to the base version and renders the prompt; no agent runs. Then with the key (keyed job only) and no allowed domain: `run-review --phase review` runs the harness. Afterwards leftover containers are removed and [`collect-result`](../agent-review/bin/collect-result) copies only `review.md` and `meta.json` out; it starts no process and opens the output directory one component at a time from `/`, none a symlink, then reads both files through that descriptor. |
| `publish` | write token (`pull-requests`), no model | On a fresh VM, [`publish-comment`](../agent-review/bin/publish-comment) validates and renders the review in a step without a token, then upserts the comment. If no review arrived, it warns and says why (`publish-comment explain`): a failure inside the sandbox, a review job that failed before the sandbox ran, or one that reported no step (refused by the environment's protection rules, cancelled while queued, or its runner lost). |

The trusted code is in [`agent-review/`](../agent-review/): `bin/` holds the entry points the workflow calls, `lib/agent_review/` the Python package they use (standard library only), `harnesses/` the adapters, `tests/` the tests. The scripts in `bin/` are Python. Each is a fixed template that refuses to run unless started as `/usr/bin/python3 -I -S -B <root>/bin/<name>` ([ADR 0004](adr/0004-python-runtime.md)). [`bin/probe`](../agent-review/bin/probe) prints the interpreter's version and flags; the `stub` harness runs it on the host (in `prepare`) and in the sandbox.

The generic review rules are in [`prompt.md.tmpl`](../agent-review/prompt.md.tmpl); the project-specific part is the caller's `rule_files` (listed in order) and its addendum. Extend the caller's rule files rather than the prompt. The PR title and body are included as delimited untrusted data. The prompt also asks the agent to finish within about 12 minutes, to read narrowly, to confirm each finding against the code and say how, and to keep maintainability remarks to an optional `### Nits` section of at most three lines; the comment has `### Summary`, `### Findings` (at most five), and optionally `### Nits` and `### Questions`.

## Security invariants

Callers use `pull_request_target` because fork PRs get no secrets under `pull_request`. That trigger runs with secrets next to untrusted code, so all PR content is handled inside AWF only. Keep these when changing anything here:

01. **The real key is only in AWF's API-proxy sidecar** and the environment of the `awf` step that runs the agent in the keyed `review` job; the agent sees placeholders. Never inline it into a `run:` script or pass it to another step. It comes from the caller's environment (`agent-review`), only through the caller's explicit mapping and only in the job that declares that environment, never from a repository secret (`authorize` fails if it resolves without the environment). It goes only to an endpoint of [`api-bases.txt`](../agent-review/api-bases.txt) in the pinned commit. [`tests/test-workflow-structure.py`](../agent-review/tests/test-workflow-structure.py) checks that no job without an environment references a secret, apart from that boolean check.
02. **No PR-derived byte is executed on the host, nor parsed by our code there.** No `actions/checkout`, no git on the PR tree outside the sandbox. `pr.json` is written as bytes and parsed inside the sandbox. Where the host must ask the API about the PR or its comments (`authorize`, `publish-comment post`), the JSON is handled only by `gh`'s own decoder (`gh api --jq`), and our code receives only the projected fields (shas, a permission, comment ids, logins and the first `FIRST_LINE_CHARS` characters, in `publish_comment.py`, of each comment's first line). The one exception is AWF's token-usage log, which the API-proxy sidecar writes out of the sandbox's reach; its request paths and model names come from the agent's requests, so the `Report token usage` step reads only its numeric fields. `prepare` never executes harness code either: it runs on the host with sudo, and its token can read and write the caller's Actions cache.
03. **The sandbox writes only to the review workspace** (`filesystem.allowWrite`); the rest of what AWF mounts from the host (the runner workspace, `/tmp`, `~/.config`, `~/.local/bin`, ...) is read-only to it. Exception: `~/.copilot/{logs,session-state}` stay writable, and AWF moves them into host `/tmp` after the run. So **the host is treated as tainted once `awf` returns**: steps after the agent's run use no token, run no git, use no cache action, and run only `/usr/bin` tools with a fixed shell and `PATH`; nothing they inherit (the workflow's and the job's `env:` and `defaults:`, `$GITHUB_ENV`) carries a token, and no step of the review jobs sets a variable that runs code before a step's script, such as `BASH_ENV`. Only this leaves `review`:
    - `review.md` and `meta.json`, at most 64 KiB each: regular, single-link files, copied without following symlinks or blocking on FIFOs, and only after every sandbox container has been removed (the single-link check relies on that);
    - the token counts in the run summary;
    - with `upload_audit`, AWF's own audit and proxy logs from `$RUNNER_TEMP`.

    The structure test checks those steps, what they inherit and the teardown gate ([`workflow_structure.py`](../agent-review/tests/workflow_structure.py) lists the checks).
04. **The agent has no network access** except AWF's API-proxy listener: its sandbox run allows no domain. Only the first, keyless run, which runs no agent, reaches `github.com`. AWF still allows the inference host for its sidecar in the shared egress proxy, so the agent can probably reach it directly too (not tried), without a key.
05. **Write tokens exist only in `publish`**, which runs no model and no PR code, and treats the review as hostile text. [`sanitize.py`](../agent-review/lib/agent_review/sanitize.py) states the exact rules and the reason for each: the review must be strict UTF-8; C0 controls other than tab and newline, DEL, and invisible characters are dropped; no raw HTML, comment end or link reference definition can form (GitHub strips attributes and hides some HTML content, and the review must not imitate the comment marker); character references are escaped (GitHub decodes `&#64;` and `&commat;` into mentions, `&rlm;` into an invisible character); `@mentions` are neutralised; the length is capped, never inside a character or a neutralisation, and the comment says so if and only if content was cut. C1 controls (U+0080 to U+009F) pass: GitHub's Markdown render API shows each as a visible U+FFFD, and none forms a mention, tag or reference. Rendering needs no token. The marker is matched only on the first line of the bot's own comments, never with `--edit-last`, because other tools (such as PR Agent) may post as the same bot. The comment listing is a `gh --jq` projection (id, login and a bounded prefix of the first line), so comment bodies are handled only by `gh`'s JSON decoder (invariant 2); a pull request with many thousands of comments can still exceed the output cap or the time limit, and then `post` fails without posting. In `publish`, the review never reaches a log: the rendered body goes to a file, then to the comment or, in a dry run, to the run summary; `post` prints only the comment URL, and `gh`'s errors prefixed and with `##[` broken up.
06. **Harness, prompt and AWF configuration come from this workflow's own commit** (`job.workflow_repository` at `job.workflow_sha`, the commit the caller pinned, fetched as a tarball and checked to be the same in every job), never from the PR or the caller. **The caller's configuration** (inputs and the addendum) comes from the caller's workflow commit: the default branch under `pull_request_target`, the dispatched ref under `workflow_dispatch` (which the environment rule gates). **Instruction files** matching [`instruction-files.txt`](../agent-review/instruction-files.txt) (the central list; the fetch phase fails if it is missing or holds no pattern) or the caller's `instruction_files`, and every `rule_files` entry, at any depth, are reset to the version in the PR's **base commit** (or removed): that is the branch the PR targets, not necessarily the default one. The PR's versions are kept as data for the review. A pattern's directories are reset too, so a PR cannot keep its files reachable through a link such as `.opencode -> evil/`. Whether a pattern matches a path is always git's decision (`:(glob,icase)**/<pattern>`), both in the reset and in the earlier check that some pattern covers each file the addendum names, so the two cannot disagree. Two post-conditions on the result then enforce the invariant, whatever the matching does. If either fails, the fetch phase stops before the prompt exists, with a message naming the path and the reason, and the run ends green with a "no review" warning (fail closed, advisory check):
    - every rule file and every file the addendum names (in backticks, with one of the listed extensions) is byte-identical to its base version or absent in both, with no symlinked parent directory;
    - no symlink or gitlink remains where a pattern matches, at a directory pattern's directory, or at a directory leading to a pattern with `/`. The exceptions are links of the base commit itself through which the agent can reach nothing the reset does not cover: a base gitlink with an empty directory (submodules are never fetched), a base symlink from one instruction file or directory to another that the reset covers (`AGENTS.md -> CLAUDE.md`, `.claude/skills -> ../.agents/skills`), and a base symlink that stands only where a wildcard pattern expects a directory on the way (`docs/*/RULES.md`) and leads to a regular file or to nothing. Any other link stops the run (see [What remains risky](#what-remains-risky)).

    The exact conditions are in `verify_no_linked_directories` ([`reset.py`](../agent-review/lib/agent_review/reset.py)). `tests/test-run-review.sh` and `tests/test-instruction-coverage.sh` check both post-conditions, and [`test_run_review.py`](../agent-review/tests/python/test_run_review.py) runs the fetch phase with the reset subverted (a PR-version, added or deleted rule file, a symlinked parent) to show that they stop it. Under `pull_request` (caller test mode) the caller's configuration is PR content, so this invariant does not hold there.
07. **The `review` job starts no service on the host.** The sandbox can connect to any host port bound to `0.0.0.0` through AWF's network gateway (`172.30.0.1`); on a stock runner that is only `sshd`. No `services:` containers, no daemons started in a step.
08. **AWF's logs are read only from `$RUNNER_TEMP`** (`--proxy-logs-dir`, `--audit-dir`). Without `--proxy-logs-dir`, AWF moves them to `/tmp` after the run, where the sandbox can plant fake ones.
09. **Only AWF's OpenAI listener is configured**: never an `anthropic` target or `ANTHROPIC_API_KEY`. In AWF v0.29.12 the Anthropic listener's `POST /messages` is not counted by `maxTurns`, and the key breaks the entrypoint under `filesystem.allowWrite`. The run log shows `API proxy enabled: OpenAI=true, Anthropic=false`.
10. **Pinned**: actions by commit SHA, the runner image (`ubuntu-24.04`, which is the sandbox's userland since AWF chroots into the host filesystem), the AWF version, AWF images by digest (the setup action's `image-tag` output, taken from the attested release at run time), and each harness by exact version with an integrity check. Not pinned: the runner image's own updates, and the model behind its id.
11. **The sandbox's output cannot issue workflow commands**: every line of `awf` output is prefixed, so none starts with `::`; NUL bytes are dropped, and every `##[` in it is then broken up with a zero-width space, because the runner also accepts the legacy `##[command]` syntax anywhere in a line (verified: unfiltered, a sandbox line `##[warning]…` becomes an annotation). Harness diagnostics go through `diag` in `common.sh`, which does the same. The stub prints such a line on stdout and through `diag` in every self-test run, also with a NUL between the two `#`, and the `check` job asserts that no annotation results; the structure test checks that every `awf` call sends stdout and stderr through the filter, that nothing else in its step can print, and that `diag` is the one filtered definition ([`workflow_structure.py`](../agent-review/tests/workflow_structure.py)).
12. **Per-job least privilege under the caller's ceiling**: top-level `permissions: {}`; `authorize`, `review` and `review-keyless` read contents and pull requests, `prepare` reads contents, `publish` adds `pull-requests: write` (the structure test checks that no other job can write). The caller grants exactly `contents: read` and `pull-requests: write`; a job asking for more fails every caller at startup, so a new permission is a breaking change.
13. **Python runs only as `/usr/bin/python3 -I -S -B <root>/bin/<name>`**: the standard library only, nothing from `PYTHON*` variables, the working directory, `site`, `sitecustomize` or `.pth` files, and no bytecode written. The root is `${RUNNER_TEMP}/trusted` in `authorize`, `prepare` and `publish`, and the root-owned `/opt/agent-review` in the review jobs. Entry points exit unless started with those flags; processes start from argument lists, with tool lookup and environment per trust zone (no process starts on the tainted host today; one that did would get an absolute program and a fixed environment); only `authorize` and `publish-comment` can reach the `gh` wrapper (which steps hold the token is decided by their `env:`); no Python file or Python shebang exists outside `lib/agent_review/` and `tests/` ([ADR 0004](adr/0004-python-runtime.md)). The structure test checks the call sites, the entry points, the imports and the tree ([`workflow_structure.py`](../agent-review/tests/workflow_structure.py)).

## What remains risky

- **A base that links an instruction file or directory to a path no reset pattern covers gets no review** (fail closed, invariant 6), because the agent would read the PR's version of the target: for example `AGENTS.md -> CONTRIBUTING.md`, `.github/copilot-instructions.md -> ../docs/copilot.md`, a rule file `docs/RULES.md -> guide.md`, or `.claude/skills -> ../tools/skills`. The run ends green with a "no review" warning, and the fetch log names the link, its target and the fix: add the target to the caller's `instruction_files` input (a file as `CONTRIBUTING.md`, a directory as `tools/skills/`), which resets it to the base version as well. Patterns apply at any depth and case-insensitively, so `CONTRIBUTING.md` resets every file of that name. This is an accepted risk: the failure is loud, and the fix is one line in the caller's `instruction_files`, where the caller sees which target a link adds; the reset does not follow links itself.
- **The PR's `.gitattributes` applies to the checkout.** A PR adding `* -diff` makes git write `Binary files differ` instead of the changes into the context's `diff.patch`, which steers what the agent sees of the diff (it can still read the files); this breaks no invariant. Attributes such as `eol=crlf` or `working-tree-encoding` also transform the base files the reset restores: a rule file then is not its base version, so the run stops (fail closed); another instruction file keeps the base text, transformed, never the PR's (tests in `test_run_review.py`).
- **Prompt injection can steer the review's content**: a false approval, misleading findings, links. Mitigations: the comment is labelled advisory, never a status, names the reviewed commit and says the PR can steer it; mentions are neutralised and the size is capped. Cross-references such as `owner/repo#123` are not neutralised and can create backlinks in other repositories. The caller's rule files are reset to base (invariant 6), but files they in turn point at (`CONTRIBUTING.md`, the ADRs) are not, so injected text there is still read as context; it can only steer review content. Visible content (links, images) is not filtered; raw HTML such as `<details>` cannot form. **Markdown-level hiding remains**, as an accepted risk: math such as `$\phantom{text}$` or `$\color{white}{text}$`, image alt text, link titles (`[a](u "text")`), link destinations in angle brackets (`[](<text with spaces>)`), and code-fence info strings (the words after an opening ```` ``` ````) carry text that the comment does not show. Text a reader cannot see can only matter to a program reading the comment, and the pull request can already put visible steering text into the review; closing these would mean parsing Markdown in the job that holds the write token.
- **A steered review can carry command lines for other bots.** The workflow itself uses no comment commands, but the review text may contain lines such as `/approve`, `/lgtm` or `bors r+`, which the sanitiser passes unchanged. `publish` posts with `github.token`, and events created with `GITHUB_TOKEN` start no workflow runs (`issue_comment` included), so no Actions-based bot can react, PR Agent in the same repository included. Only a bot that receives webhooks (an App, Prow, bors) could, and whether it acts depends on its own checks of the comment's author (here the workflow's bot account); check those before adopting. This is an accepted risk: such commands have no common form (`bors r+` starts with no `/`), so breaking them up would need an open-ended denylist, and the pull request's author can post the same line directly.
- **Budget burn, driven by the content of any reviewed PR.** The agent has a shell and can call AWF's listener directly, so the per-run bound is AWF's rate limiter, not the turn cap: at most **100 upstream requests per run** (`rateLimiting.requestsPerHour`; the run is shorter than an hour), paced at 30 per minute, each up to the model's context and output limits. AWF's `maxTurns` (80) stops a well-behaved harness but is not a hard cap: it counts only `POST`s to chat-completions, responses and messages paths, when the response ends. Other paths are forwarded with the real key and not counted (the key works for `/v1/completions`), and concurrent calls are all admitted before any is counted. AWF forwards every method and path under `/v1` (dot segments cannot leave `/v1`; encoded separators such as `%2f` are passed through), has no `max_tokens` clamp, and skips the model allowlist when the body has no string `model` or is not JSON. All of this was checked in a test fork against a fake upstream: 100 concurrent chat calls all reached it under a turn cap of 20, and 100 calls to uncounted paths reached it under the production limits before the hourly limit answered 429. Hence the dedicated key's budget (Required settings, item 2) is the real cap.
- **A sandbox escape** would expose the key on the host. Beyond kernel and Docker bugs, this includes AWF itself: it runs as root on the host, and its API-proxy sidecar, which holds the key, parses requests from the sandbox; both are young code (the AWF probe worked around three of its bugs). Mitigations: the dedicated, low-budget key, quick to rotate; gVisor (`--container-runtime gvisor`) is planned once the harnesses are known to work with it.
- **Egress**: the keyless fetch run reaches `github.com` and its subdomains, with git only. The agent's run reaches the API proxy, the inference host (without a key) and the runner's `sshd` through AWF's gateway, and runs for up to 25 minutes.
- **Live logs**: the review step's log shows the agent's output (prefixed) while it runs; it carries no secret.
- **Central changes reach every caller that updates its pin.** Callers pin a full SHA and review Dependabot's update PRs; releases flag new permissions, inputs, ceilings, endpoints, harness pins and AWF versions as breaking. The protection of `main` (pull request with an approval, no force pushes) does not by itself protect what callers pin: tags, releases and commits pushed only to a fork are outside it, and required status checks are a separate setting. Callers must check each pin ([Pinning and updating](#pinning-and-updating)).
- **Compromise of this repository's `main` or of one admin account.** Admins cannot bypass the ruleset but can edit it. A SHA-pinned caller is unaffected until it updates its pin, so the review of the pin update is the only gate, and the central diff is not shown in the caller's PR. A tag- or branch-pinned caller is hit on its next run. A malicious central commit gets, in a caller: the inference key (in `review`), `pull-requests: write` (in `publish`), and write access to the caller's Actions cache in the default-branch scope under `pull_request_target`, which other workflows restore. It gets no other secret, thanks to the explicit mapping.
- **Tag and branch pins.** The ref is resolved once per run; every job fetches by SHA and checks that it runs the recorded commit, so one run never mixes commits (verified for tag and branch pins). A re-run after the tag moved re-resolves the called workflow's tag (it runs the moved commit) while keeping the caller's commit, so a tag pin is not reproducible, not even across attempts of one run; only SHA pins are.
- **Inputs are as trusted as the caller's workflow file** on its default branch. Never derive `environment`, `model`, `rule_files`, `instruction_files`, `prompt_addendum`, `label` or `api_base` from an event payload, a dispatch input or a repository variable: whoever can change a variable needs no review. The endpoint allowlist limits where the key can go, but not the other inputs.
- **Caller test mode** (`pull_request`, `stub`): the caller's workflow file, inputs, rule files and addendum are PR content there, and fork PRs are protected by GitHub's rules (read-only token, no secrets), not by this workflow. It proves the plumbing only, never the reset invariant.
- **The self-test's "nothing can be spent or posted"** holds inside the reviewed file: stub, keyless job, forced dry run. For a fork or branch PR of this repository the file itself is the PR's, so the guarantee there is GitHub's fork rules plus the absence of secrets in this repository, not these checks. **Never store a key or any secret in this repository**: keyed tests run from a separate test caller.
- **One keyed review per PR at a time**: the keyed job's concurrency group queues a second run; a newer queued run replaces an older queued one, which GitHub cancels.
- **AWF updates are manual**: Dependabot ignores `github/gh-aw-firewall` ([`dependabot.yml`](../.github/dependabot.yml)), so its security fixes reach this workflow only when a maintainer bumps it; watch its releases and advisories.
- `@`, `&`, `]:` and `<` before a letter, `/`, `!` or `?` are neutralised everywhere, including code blocks, so a Python decorator or `std::vector<int>` copied from a review contains an invisible character, and character references show literally.

## Troubleshooting

**The token-usage table** in the run summary of the keyed `review` job counts the rows of AWF's `token-usage.jsonl`. AWF v0.29.12 writes a row only for successful (2xx) responses, so "Model calls" counts successful calls and "Failed calls" stays 0. A request rejected upstream (for example `403` for an invalid key) or one that timed out does not appear. Diagnose those from the harness's error output in the `Run the review in the sandbox` log, or with an audit run, where `token-tracker-audit.jsonl` records non-2xx responses as `skip_status` and `otel.jsonl` shows each request's duration.

**Audit and proxy logs** (`upload_audit: true`) show what the sandbox connected to and which model was called. AWF redacts the key in the compose environment and logs no request headers, but the artifact (kept 7 days) is readable by any signed-in user of a public repository, so treat it as public. Never set the input in the standing caller configuration. Pass it for one run, through a dispatch input mapped to `upload_audit`, or a separate dispatch-only workflow that calls this one with the same pin, `dry_run: true` and the same environment and secret mapping.

**"No review was posted"** (the `Agent review failed` warning of `publish`, also in the run summary) names the cause:

- _the agent produced no review_ (with the fetch and review outcomes and the exit code): a failure inside the sandbox; the cause is in the `Fetch the pull request in the sandbox` or `Run the review in the sandbox` log of the review job.
- _the review job failed_ (or _was cancelled_) _before the sandbox ran_: see the failed step of that job (for example a `prompt_addendum` path that does not exist).
- _the `review` job reported no step: it never got a runner, or its runner was lost_: for the keyed job the usual cause is that the environment refused it, through one of its protection rules: the deployment branch rule (a keyed dispatch from a branch other than the default one), a required reviewer who rejected or did not answer, or another rule. GitHub shows that as an annotation on the `review` job and as a failed deployment; the key was then not used and there are no sandbox logs. See [Required settings](#required-settings-in-the-calling-repository), item 1. A runner lost during the job (out of memory, disk full, shut down) reports no step either, and cannot be told apart from here; the job's own page shows which it was. `publish` also shows an `Artifact not found` error annotation from its download step; it is a consequence, not the cause. The same message for the `review-keyless` job, which has no environment, means that it got no runner or lost it.
- _the review job was cancelled before it reported a step_: someone cancelled the run or, for the keyed job only, a newer review request for the same pull request replaced this one in its queue.

**Slow or missing keyed reviews: requests that stall.** A request to the inference service sometimes gets no answer for minutes: keyed reviews have taken 17 to 19 of the 20 minutes of `harness_timeout_minutes`; the proxy log of one of them shows two requests ended after 300 s without response headers and one answered after 185 s (the others are assumed to share the cause). Each adapter bounds these waits without shortening how long a run waits for a silent service; the values and their reasons are in its `run.sh`:

- A **streamed response that stops** is ended and the request retried, after `CHUNK_TIMEOUT_MS` in opencode and `IDLE_TIMEOUT_MS` in Pi. A response that keeps sending is never cut, however long it takes. The same wait applies between the response headers and the first event, where a service that queues an accepted request, or a model that thinks before its first token, is silent too; that is why it is not shorter.
- A request that gets **no response headers** is ended and retried after `HEADER_TIMEOUT_MS` in opencode, at most 3 times per run (a limit that cannot be configured), and after `IDLE_TIMEOUT_MS` in Pi, up to `MAX_RETRIES` times.

So a stream that stalls is recovered, but a long silence before the first response headers still makes the review slow, and when the service answers nothing for longer than about 15 minutes the run ends at the harness time limit with the "no review" warning (the log says `status 'timeout'`; the run stays green). Re-request the review. That silence is the service holding requests, which no setting here can shorten: if it happens regularly, raise it with the service's operators (one possible cause, not verified, is a rate limit of the key that holds requests instead of refusing them). The time limit is a ceiling, and raising it is a breaking change. Every retried request counts against the run's request limits (`requests_per_hour`). One that ends before any response does not count against `max_turns`; whether a stream cut after its headers does was not checked.

Known limitation (no workaround in the workflow):

- **The reset logs no per-file list.** `run-review` logs that the rule files and the files the addendum names are their base versions, but not which instruction files it removed or kept as data.

## Configuration notes

**Recommended**: `opencode` with `zai-org/GLM-5.3-Flash`; `pi` with the same model is the alternative (more findings per PR, 1.5–2× the tokens). `zai-org/GLM-5.3` (non-Flash) with opencode is not recommended at the current caps: on real PRs it produced no review, timing out at the 20-minute harness limit. This is from a quality evaluation of dry runs on three merged PRs of a large Python project.

Ceilings, changed only by a reviewed PR here: the request rates to the API proxy (the per-run bound), `apiProxy.maxTurns` and AWF's `agentTimeout` in [`awf.yaml`](../agent-review/awf.yaml), and the time limits of the review step and job in the workflow. Callers can lower the request rates, the turn cap and the harness time limit (see [Inputs](#inputs)). `maxTurns` stops a well-behaved harness; opencode also stops itself earlier (`MAX_STEPS` in its `run.sh`), Pi has no cap of its own. AWF's AI-credit cap is not used, since it prices CSCS models with an estimate, after the response.

AWF's runtime model catalogue is disabled ([`awf.yaml`](../agent-review/awf.yaml) says why). The inference host (added via `baseUrlEnv`) is marked _sensitive_ by AWF and is therefore excluded from Squid's `access.log` and `audit.jsonl`, so model traffic does not appear in the egress logs by design; it is visible only in the API-proxy's own `token-usage.jsonl`.

The `stub` harness makes no model call and posts a fixed review containing hostile content; it is for testing the pipeline only.

## Harness adapters

A harness is a directory `agent-review/harnesses/<name>/` plus an entry in the harness allowlist of [`lib/agent_review/authorize.py`](../agent-review/lib/agent_review/authorize.py), as keyed (it calls a model) or keyless; callers may add it to their `workflow_dispatch` choice. The [adapters README](../agent-review/harnesses/README.md) holds the contract (`install.sh`, `run.sh`, `wire`) and compares the harnesses; each adapter's `README.md` explains its pins, upgrades and isolation. File names a harness reads as instructions or configuration, at any depth, go into [`instruction-files.txt`](../agent-review/instruction-files.txt).

## Testing changes

This repository's [`self-test.yml`](../.github/workflows/self-test.yml) runs on every pull request, on `main`, weekly and on demand:

- pre-commit ([`.pre-commit-config.yaml`](../.pre-commit-config.yaml)) and [zizmor](https://docs.zizmor.sh/) on the workflows;
- the script suites in [`agent-review/tests/`](../agent-review/tests/) (`test-*.sh`), the black-box specification of each entry point's command-line contract (arguments, exit codes, outputs, notices): they run each `bin/` entry point through `run_entry` in [`tests/lib.sh`](../agent-review/tests/lib.sh), with the production Python command line. All are offline except `test-run-review.sh`, which fetches real commits from github.com (no AWF);
- the [workflow structure test](../agent-review/tests/test-workflow-structure.py), which enforces the structural parts of the invariants, the Python runtime rules of invariant 13 included ([`workflow_structure.py`](../agent-review/tests/workflow_structure.py) lists every check);
- the `python` job: the pytest suites in [`tests/python/`](../agent-review/tests/python/) and mypy, with the test tools of the hash-pinned [`requirements.txt`](../agent-review/tests/requirements.txt). They cover the package's helpers, cases the script suites cannot reach (such as `collect-result`'s simulated swaps between check and open), the review sanitiser against an independent oracle and with property tests, `run-review` on crafted pull requests served from a local repository, each entry point run with the production command line in a hostile environment (planted modules in the working directory, hostile `PYTHON*` variables), and the structure checks on crafted inputs. Locally: `python3 -B -m pytest` and `python3 -m mypy` from the repository root;
- the whole pipeline through a **local** call (`uses: ./.github/workflows/agent-review.yml`), so the files fetched at `job.workflow_sha` are the commit under test, with the `stub` harness (keyless job), no key and a forced dry run, against a fixed public PR of another repository (`test_repository`); the dry-run comment is in the run summary;
- the `check` job: the keyed job skipped and the keyless one run, nothing posted, the sandbox's allowed domains, the clamped caps, no key in AWF, the reset post-condition, no deployment for the commit, `gh` found as `/usr/bin/gh` under the publish steps' fixed `PATH` (the self-test never posts), and the probe's line from the host (`prepare`) and from inside the sandbox: the same interpreter, the isolation flags, no `site`, only the standard library and the trusted `lib/` on `sys.path`.

The self-test path (`test_repository`, and the `push` and `schedule` events) is accepted only with the `stub` harness when `job.workflow_repository` equals the calling repository: this repository, or a fork or copy calling its own file, which is harmless (stub, no key, dry run). It skips the writer check, since nothing can be spent or posted; the intent is that Dependabot's and outside contributors' PRs can run it too (not yet observed: their read-only token might still fail the permission ceiling at startup).

The self-test is a local call, where `job.workflow_repository` and `job.workflow_sha` equal the caller's own `github.*` values, so it cannot notice a regression that confuses them (such a regression fails closed); only a cross-repository call can (see [Release checklist](#release-checklist)).

To try an adapter without a key, run `install.sh <prefix>`, then `run-review` with `AWF_API_PROXY_OPENAI_URL` pointing at a local fake OpenAI server. [`tests/test-harness-stall.sh`](../agent-review/tests/test-harness-stall.sh) does this for the request timeouts, with a server that stalls ([`tests/fake_inference.py`](../agent-review/tests/fake_inference.py)); its header lists the cases, the options and what it needs. It runs the real harnesses on the local machine, outside any sandbox, so it is deliberately not part of the self-test (whose `adapters` job only installs them): run it locally after changing an adapter's timeouts or its pinned version.

### Caller test mode

Any caller may call the workflow under `pull_request` with the **`stub`** harness only (the job in [Calling it](#calling-it)): the dry run is forced by the event name, there is no writer check, and the run uses the keyless job, so it needs no key and no environment and is not limited to the default branch (verified from another repository; not observed for fork PRs, whose read-only token might fail the permission ceiling at startup). If the caller's Actions event policy for its workflow file exists, it must list `pull_request` too: `allowed_events` is an exhaustive allowlist. A keyed harness is rejected under `pull_request` (and under every event but `pull_request_target` and `workflow_dispatch`), and `test_repository` stays limited to this repository. A notice says what follows: the caller's workflow file, its inputs, rule files and addendum are **PR content** in this mode (the caller's `github.workflow_sha` is the PR's merge commit). The mode proves the plumbing (the cross-repository call, the inputs, the addendum fetch, the sandbox, the dry-run rendering), never the reset invariant.

## Releases

Releases cover every workflow in this repository; PR Agent's additional checks are in its [release checklist](pr-agent.md#release-checklist). Semver tags `vX.Y.Z` on reviewed commits of `main`, with release notes that flag **breaking** changes: a new permission in any job, a new required input, a raised ceiling, a new endpoint in `api-bases.txt`, a new harness pin or AWF version. No moving major tag (`v1`): it would let changes reach callers unreviewed. The first release is **`v0.1.0`**.

### Release checklist

Before tagging (`v0.1.0` and every later release):

1. The self-test is green on the commit to be tagged, which is on `main` (merged through a reviewed PR).
2. Repository settings are in place: tag ruleset for `v*` (restricted creation, no update, no deletion), immutable releases, the self-test jobs as required status checks on `main`, no repository or organization secret available to this repository.
3. Cross-repository tests from a **separate test caller repository** pinned to the release candidate's SHA on `main` (its own `agent-review` environment, default branch only, admins not exempt, with a dedicated low-budget key; never a key here; an Actions event policy for its workflow file allowing `pull_request_target`, `workflow_dispatch` and `pull_request`, without which `pull_request_target` is blocked from 2 December 2026; no forced dry run left in its workflow file):
   1. `workflow_dispatch` dry run with `stub` from the default branch: the notice shows `workflow GridTools/ci-workflows@<sha>; caller <test repo>@<sha>` with distinct values, the keyless job runs, no deployment is recorded.
   2. `pull_request` caller test mode with `stub`: dry run forced, the PR-content notice; with `opencode`: rejected in `authorize` by the event rule, which needs the test job to pass a `model` (otherwise the model check fires first: "No model given ...").
   3. The label path on a fixture PR (`pull_request_target`, `labeled`), with a writer: a real post through `/usr/bin/gh` under the publish steps' fixed `PATH: /usr/bin:/bin`, updated in place on a second run.
   4. A non-writer adding the label (or the PR's author without write) is denied with a notice. The labeller variant needs an organization repository, where the `triage` role exists (a read-only user cannot add labels); in a user-owned repository only the PR-author variant (an automatic type, with the job `if` extended as in [Usage](#usage)) can be tested.
   5. A keyed dispatch from a branch other than the default: denied by the environment rule before getting a runner.
   6. One keyed dry run per harness (`opencode`, `pi`) with the dedicated key: a rendered review, the token-usage table, `OpenAI=true` only in the review run.
   7. A shared repository secret named like the mapped one makes `authorize` fail (then delete it).
   8. A pin to a commit that exists only in a fork of this repository: record what the notice prints, and that the caller-side check in [Pinning and updating](#pinning-and-updating) rejects it.
   9. A tag-pinned call, re-run after the tag moved (in a scratch repository, never with release tags here): record which commit the re-run uses.
   10. A PR from a fork, by an account without write access, labelled by a writer (`pull_request_target`): the keyed review runs on the fork's head inside the sandbox, the instruction-file reset and its post-conditions hold, and the comment is posted; nothing from the fork runs on the host.
   11. Invariant 6 with a file the addendum names in backticks: a PR that changes that file (the fetch log says `1 file(s) named by the addendum are their base versions`, and the comment is posted), and a PR that replaces it with a symlink to a path no reset pattern covers (the reset restores the base file: the same log line, and the comment is posted). `stub` is enough.
4. Release notes list breaking changes and the evidence (run URLs) of step 3.
5. Callers update their pin to the tag's SHA and check it ([Pinning and updating](#pinning-and-updating)).
