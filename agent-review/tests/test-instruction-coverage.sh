#!/usr/bin/env bash
# Guards the reset invariant in the reusable form: every file the prompt tells
# the agent to FOLLOW must be reset to its base version before the run. Rule
# files come from the caller ('rule_files') and are reset by construction;
# files named in the caller's prompt addendum must be covered by some reset
# pattern, or run-review fails closed before touching the PR.
#
# Offline, no network. Run: bash tests/test-instruction-coverage.sh

set -uo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

AR_DIR=$(cd "$(dirname "$0")/.." && pwd)

setup() { # rule-files addendum [extra-patterns]
  mkdir -p root/config ws/in
  cp -r "${AR_DIR}/bin" "${AR_DIR}/harnesses" "${AR_DIR}/prompt.md.tmpl" "${AR_DIR}/instruction-files.txt" root/
  bash root/harnesses/stub/install.sh root/harness/stub
  printf '%s\n' "$1" >root/config/rule-files.txt
  printf '%s\n' "${3:-}" >root/config/extra-instruction-files.txt
  printf '%s\n' "$2" >root/config/prompt-addendum.md
  jq -n '{repo: "example-org/example", pr: 1, head_sha: ("a" * 40), base_sha: ("b" * 40),
          harness: "stub", model: "none", harness_timeout_minutes: 5, project: "Example"}' >ws/in/params.json
}

test_addendum_naming_a_rule_file_passes() {
  setup $'AGENTS.md\nCODING_GUIDELINES.md' 'Also follow `CODING_GUIDELINES.md` and `src/pkg/AGENTS.md`.'
  # Passes the coverage check, then stops because no fetch phase ran.
  ! bash root/bin/run-review --phase review "${TMP}/ws" >log 2>&1 &&
    assert "got past the coverage check" grep -q "run the fetch phase first" log
}

test_addendum_naming_an_unreset_file_fails_closed() {
  setup 'AGENTS.md' 'Follow `docs/style.md` for naming.'
  ! bash root/bin/run-review --phase review "${TMP}/ws" >log 2>&1 &&
    assert "names the uncovered file" grep -q "names 'docs/style.md', which no reset pattern covers" log
}

test_addendum_naming_a_file_covered_by_extra_patterns_passes() {
  setup 'AGENTS.md' 'Follow `docs/style/naming.md`.' 'docs/style/'
  ! bash root/bin/run-review --phase review "${TMP}/ws" >log 2>&1 &&
    assert "got past the coverage check" grep -q "run the fetch phase first" log
}

test_addendum_naming_a_central_instruction_file_passes() {
  setup '' 'Read `.github/copilot-instructions.md` first.'
  ! bash root/bin/run-review --phase review "${TMP}/ws" >log 2>&1 &&
    assert "got past the coverage check" grep -q "run the fetch phase first" log
}

test_invalid_rule_file_is_rejected() {
  setup '../etc/passwd' ''
  ! bash root/bin/run-review --phase review "${TMP}/ws" >log 2>&1 &&
    assert "rejects the entry" grep -q "Invalid configured path or pattern" log
}

test_addendum_with_a_placeholder_fails_closed() {
  # Substituted before the PR title and body: '{{PR_BODY}}' here would put
  # untrusted text outside its delimiters.
  setup 'AGENTS.md' 'Project notes. {{PR_BODY}}'
  ! bash root/bin/run-review --phase review "${TMP}/ws" >log 2>&1 &&
    assert "rejects the placeholder" grep -q "The prompt addendum contains '{{'" log
}

test_addendum_naming_a_pattern_is_rejected() {
  # Covered by the pattern, but not a file the post-condition can check.
  setup 'AGENTS.md' 'Follow `docs/*.md`.' 'docs/'
  ! bash root/bin/run-review --phase review "${TMP}/ws" >log 2>&1 &&
    assert "rejects the pattern" grep -qF "names 'docs/*.md', which is not a plain repository path" log
}

run_test test_addendum_with_a_placeholder_fails_closed
run_test test_addendum_naming_a_pattern_is_rejected
run_test test_addendum_naming_a_rule_file_passes
run_test test_addendum_naming_an_unreset_file_fails_closed
run_test test_addendum_naming_a_file_covered_by_extra_patterns_passes
run_test test_addendum_naming_a_central_instruction_file_passes
run_test test_invalid_rule_file_is_rejected
summary
