#!/usr/bin/env bash
# Unit tests for bin/resolve-models: every validation, the defaults and the
# derived step timeout. Offline.
#
# Run: bash pr-agent/tests/test-resolve-models.sh

set -uo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
PA_DIR=$(cd "$(dirname "$0")/.." && pwd)

setup() {
  export GITHUB_OUTPUT="${TMP}/out" JOB_TIMEOUT=45 DOCS_URL=''
  export IN_MODEL=zai-org/GLM-5.3-Flash IN_FALLBACKS='Qwen/Qwen3.5-397B, zai-org/GLM-5.3'
  export IN_BUDGET='' IN_OUTPUT='' IN_AI_TIMEOUT=''
}
run() { bash "${PA_DIR}/bin/resolve-models" >log 2>&1; }
out() { sed -n "s/^$1=//p" "${GITHUB_OUTPUT}" | tail -n 1; }
ok() { run || { cat log; return 1; }; }
rejected() { # expected error text
  if run; then
    cat log
    echo "    expected a failure"
    return 1
  fi
  assert "says '$1'" grep -qF -- "$1" log && assert "points at the docs" grep -qF "docs/pr-agent.md#inputs" log &&
    assert "no outputs" test ! -s "${GITHUB_OUTPUT}"
}

test_defaults() {
  setup
  ok &&
    assert "model" test "$(out model)" = openai/zai-org/GLM-5.3-Flash &&
    assert "fallbacks" test "$(out fallbacks)" = '["openai/Qwen/Qwen3.5-397B","openai/zai-org/GLM-5.3"]' &&
    assert "budget" test "$(out budget)" = 262144 &&
    assert "output" test "$(out output)" = 100000 &&
    assert "ai timeout" test "$(out ai_timeout)" = 720 &&
    assert "3 x 720 s + 2 min" test "$(out step_timeout)" = 38 &&
    assert "notice" grep -qF "Model 'openai/zai-org/GLM-5.3-Flash'" log
}
test_explicit_values() {
  setup
  export IN_BUDGET=131072 IN_OUTPUT=32768 IN_AI_TIMEOUT=61 IN_FALLBACKS=none
  ok && assert "no fallbacks" test "$(out fallbacks)" = '[]' &&
    assert "budget" test "$(out budget)" = 131072 && assert "ceil(61/60) + 2" test "$(out step_timeout)" = 4
}
test_fallbacks_trimmed() {
  setup
  export IN_FALLBACKS=$' a ,, b\n'
  ok && assert "trimmed, empty dropped" test "$(out fallbacks)" = '["openai/a","openai/b"]'
}
test_fallbacks_only_commas() { setup; export IN_FALLBACKS=' , '; rejected "Input fallback_models lists no model"; }
test_missing_model() { setup; export IN_MODEL=''; rejected "Input model is empty (no default)."; }
test_missing_fallbacks() { setup; export IN_FALLBACKS=''; rejected "Input fallback_models is empty; use 'none' for no fallback."; }
test_invalid_models() {
  local m
  for m in '@format {env[OPENAI.KEY]}' 'a b' $'a\nb' '"x"' '[x]' '-x' "$(printf 'm%.0s' {1..129})"; do
    setup; export IN_MODEL=${m}; rejected "Invalid model id" || { echo "    model: '${m}'"; return 1; }
    # A newline separates fallbacks, like a comma.
    [ "${m}" = $'a\nb' ] && continue
    setup; export IN_FALLBACKS="ok, ${m}"; rejected "in input fallback_models" || { echo "    fallback: '${m}'"; return 1; }
  done
}
test_not_positive() {
  local v name
  for name in token_budget max_output_tokens ai_timeout; do
    for v in 0 00 0123 010 -1 1.5 1e3 ' 1' abc 1234567890; do
      setup
      case "${name}" in
        token_budget) export IN_BUDGET=${v} ;;
        max_output_tokens) export IN_OUTPUT=${v} ;;
        ai_timeout) export IN_AI_TIMEOUT=${v} ;;
      esac
      rejected "Input ${name} must be a positive integer without leading zeros, got '${v}'." || return 1
    done
  done
}
test_timeout_over_job_limit() { setup; export IN_AI_TIMEOUT=2701 IN_FALLBACKS=none; rejected "Input ai_timeout ('2701' s) exceeds the 45 min job limit."; }
test_budget_not_above_output() { setup; export IN_BUDGET=100000; rejected "Input token_budget ('100000') must be greater than max_output_tokens ('100000')."; }
test_chain_does_not_fit() { setup; export IN_AI_TIMEOUT=900; rejected "3 models at '900' s need 47 min, leaving no margin under the 45 min job limit."; }
test_chain_just_fits() {
  # 1 model: ceil(2460/60) + 2 = 43, and 43 + 2 = 45.
  setup; export IN_AI_TIMEOUT=2460 IN_FALLBACKS=none
  ok && assert "43 min" test "$(out step_timeout)" = 43
}
test_chain_just_misses() { setup; export IN_AI_TIMEOUT=2461 IN_FALLBACKS=none; rejected "1 models at '2461' s need 44 min"; }
test_caller_docs_in_errors() { setup; export DOCS_URL=https://example.org/doc IN_MODEL=''; rejected "#inputs and https://example.org/doc."; }
test_bad_job_timeout() { setup; export JOB_TIMEOUT=''; rejected "Invalid JOB_TIMEOUT"; }

for t in $(declare -F | awk '{print $3}' | grep '^test_'); do run_test "${t}"; done
summary
