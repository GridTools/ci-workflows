# opencode adapter

[opencode](https://github.com/anomalyco/opencode) (MIT), the v2 line published
as `@opencode/cli` on npm (`opencode-ai` is the older v1 line, a different
product). Wire: OpenAI chat, through AWF's OpenAI listener.

## Version and upgrades

Pinned to 2.2.7 through `package.json` and `package-lock.json` (npm integrity
hash). The adapter installs the platform package
`@opencode/cli-linux-x64-baseline` directly: the `@opencode/cli` wrapper only
hard-links that same binary in a postinstall script, and its lockfile pulls
the binaries of every platform (992 MB instead of 247 MB). `baseline` also
runs on CPUs without AVX2. Linux x64 only.

To upgrade, set the version in `package.json`, then regenerate the lockfile
with `npm install --package-lock-only --ignore-scripts` and check that the
release still has the flags and environment variables used in `run.sh`.

## Configuration and isolation

- The configuration comes from `OPENCODE_CONFIG_CONTENT` only: one provider
  (`awf-proxy`, base URL `$AWF_API_PROXY_OPENAI_URL/v1`, placeholder key), a
  policy denying every other provider, `share` and `update` disabled.
- `OPENCODE_DISABLE_PROJECT_CONFIG=1`: the repository's `opencode.json(c)`,
  `.opencode/` and `AGENTS.md` are ignored. Without it, `.opencode/plugins/*.js`
  are **executed** and MCP servers in `opencode.json` are **spawned**.
- Even with it, a nested `AGENTS.md` is attached to the result of a `read` in
  its directory, which is why `run-review` resets instruction files at any
  depth.
- Denied tools: `edit`, `webfetch`, `websearch`, `question`, `subagent`,
  `execute`, `opencode_*`. Left: `glob`, `grep`, `read`, `shell`, `skill`.
  `shell` is not restricted (an allowlist is possible but would block
  harmless read-only commands); the sandbox is the boundary.
- `agents.build.steps` caps a run at 35 model steps, below AWF's
  `apiProxy.maxTurns`. opencode makes one more request first, for the session
  title. At the last step tools are disabled, so the model has to answer.

- **Request timeouts** are provider settings, both 300 s. `headerTimeout`
  is the wait for response headers (opencode's default, set explicitly).
  `chunkTimeout` is the wait for the next event of a streamed response,
  including the first one after the headers (the default is 900 s, most of
  the harness timeout); a stream that keeps sending is never cut. opencode
  retries a timed-out request after 2, 4 and 8 s, but at most 3 times per
  run, and that limit cannot be configured: a run waits at most 4 x the
  timeout for a service that answers nothing. That is why neither value is
  lower: in the v0.1.0 release validation, a review succeeded after 13
  minutes without an answer, and one request was answered after 185 s
  without headers. A service that sends its headers at once and then
  queues the request is silent in the same way under `chunkTimeout`, so it
  gets the same wait. `../../tests/test-harness-stall.sh` checks this
  against a server that stalls; run it after changing these values or the
  pinned version, since the retry limit may change with it.

## Outbound calls

`models.opencode.ai` (model catalogue), suppressed with
`OPENCODE_DISABLE_MODELS_FETCH=1`. With it, opencode connects only to the
listener and probes local model servers on loopback (Ollama, LM Studio,
vLLM ports), which is harmless.

## Answer and exit codes

`opencode run --format json` writes JSONL. The review is the text after the
last `tool_use` event (`run-review` then drops anything before its first
`### ` heading, such as a note that the step cap was reached). opencode exits 0 when the step cap ends a run without
an answer (then `run.sh` exits 2) and 1 when the endpoint is unreachable.
