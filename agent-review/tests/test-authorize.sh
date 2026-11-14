#!/usr/bin/env bash
# Unit tests for bin/authorize: input validation, the event and harness
# matrix, the writer check and the outputs. Offline: 'gh' is a fake on PATH
# that answers the two API calls the script makes.
#
# Run: bash tests/test-authorize.sh

set -uo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

AR_DIR=$(cd "$(dirname "$0")/.." && pwd)
CENTRAL=GridTools/ci-workflows
CALLER=example-org/example
CSCS=https://api.inference.cscs.ch/v1
HEAD=$(printf 'a%.0s' {1..40})
BASE=$(printf 'b%.0s' {1..40})
WF_SHA=$(printf 'c%.0s' {1..40})
CALLER_SHA=$(printf 'd%.0s' {1..40})

# A valid 'workflow_dispatch' of a keyed harness from a caller; tests change
# what they need.
setup() {
  mkdir -p fakebin
  cat >fakebin/gh <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >>"${TMP}/gh.log"
[ -z "${FAKE_GH_FAIL:-}" ] || { echo "HTTP 502" >&2; exit 1; }
case "$2" in
  */pulls/*) printf '%s\t%s\n' "${FAKE_HEAD}" "${FAKE_BASE}" ;;
  */collaborators/*/permission) echo "${FAKE_PERM}" ;;
  *) echo "unexpected call: $*" >&2; exit 1 ;;
esac
EOF
  chmod +x fakebin/gh
  export PATH="${TMP}/fakebin:${PATH}" GITHUB_OUTPUT="${TMP}/out"
  export FAKE_HEAD=${HEAD} FAKE_BASE=${BASE} FAKE_PERM=write
  export REPO=${CALLER} EVENT=workflow_dispatch ACTION='' AUTHOR=author SENDER=sender ACTOR=actor
  export EVENT_PR='' EVENT_HEAD_SHA='' EVENT_BASE_SHA='' EVENT_DRAFT='' EVENT_LABEL=''
  export IN_PR=7 IN_HARNESS=opencode IN_MODEL=zai-org/GLM-5.3-Flash IN_DRY_RUN=false
  export IN_LABEL=agent-review IN_PROJECT='' IN_RULES=$'AGENTS.md\nCODING_GUIDELINES.md'
  export IN_PATTERNS='' IN_ADDENDUM=.github/agent-review/prompt-addendum.md IN_API_BASE=${CSCS}
  export IN_RPH=100 IN_RPM=30 IN_TURNS=80 IN_TIMEOUT=20 IN_ENVIRONMENT=agent-review IN_TEST_REPO=''
  export TRUSTED_REPO=${CENTRAL} TRUSTED_SHA=${WF_SHA} CALLER_WF_SHA=${CALLER_SHA}
  export HAS_SHARED_KEY=false
}
# A 'pull_request_target' event of a caller.
setup_prt() { # action
  setup
  export EVENT=pull_request_target ACTION=$1 EVENT_PR=12 EVENT_HEAD_SHA=${HEAD} EVENT_BASE_SHA=${BASE}
  export EVENT_DRAFT=false IN_PR=''
  [ "$1" != labeled ] || export EVENT_LABEL=agent-review
}

run() { run_entry "${AR_DIR}/bin/authorize" >log 2>&1; }
out() { sed -n "s/^$1=//p" "${GITHUB_OUTPUT}" | tail -n 1; }
allowed() { run || { cat log; return 1; }; assert "allowed" test "$(out allowed)" = true; }
denied() { # expected log text: exit 1, allowed=false
  if run; then
    cat log
    echo "    expected a failure"
    return 1
  fi
  assert "allowed=false" test "$(out allowed)" = false &&
    assert "says '$1'" grep -qF -- "$1" log
}
skipped() { # expected notice: exit 0, allowed=false
  run || { cat log; return 1; }
  assert "allowed=false" test "$(out allowed)" = false &&
    assert "says '$1'" grep -qF -- "$1" log
}

# --- Outputs -------------------------------------------------------------------
test_dispatch_keyed_outputs() {
  setup
  allowed &&
    assert "keyed" test "$(out keyed)" = true &&
    assert "pr" test "$(out pr)" = 7 &&
    assert "head" test "$(out head_sha)" = "${HEAD}" &&
    assert "base" test "$(out base_sha)" = "${BASE}" &&
    assert "model" test "$(out model)" = zai-org/GLM-5.3-Flash &&
    assert "dry run as given" test "$(out dry_run)" = false &&
    assert "project defaults to the repository name" test "$(out project)" = example &&
    assert "api base" test "$(out api_base)" = "${CSCS}" &&
    assert "source repo" test "$(out source_repo)" = "${CALLER}" &&
    assert "call id" grep -qE '^call_id=[0-9a-f]{16}$' "${GITHUB_OUTPUT}" &&
    assert "trusted commit" test "$(out trusted_sha)" = "${TRUSTED_SHA}" &&
    assert "actor checked" grep -qF "repos/${CALLER}/collaborators/actor/permission" gh.log &&
    assert "self-location notice" grep -qF "Agent review workflow ${CENTRAL}@${TRUSTED_SHA}; caller ${CALLER}@" log
}

test_stub_is_keyless_with_model_none() {
  setup
  export IN_HARNESS=stub IN_MODEL=''
  allowed && assert "keyless" test "$(out keyed)" = false && assert "model none" test "$(out model)" = none
}

test_pi_is_keyed() {
  setup
  export IN_HARNESS=pi
  allowed && assert "keyed" test "$(out keyed)" = true
}

# --- Harness and model -----------------------------------------------------------
test_unknown_harness() { setup; export IN_HARNESS=memdoor; denied "Unknown harness 'memdoor'"; }
test_empty_harness() { setup; export IN_HARNESS=''; denied "No harness given"; }
test_missing_model() { setup; export IN_MODEL=''; denied "No model given for harness 'opencode'"; }
test_multiline_model() { setup; export IN_MODEL=$'zai-org/GLM\nrm -rf'; denied "Invalid model id"; }
test_model_with_space() { setup; export IN_MODEL='a b'; denied "Invalid model id"; }

# --- S1: the key must be an environment secret -----------------------------------
test_shared_key_is_rejected() {
  setup
  export HAS_SHARED_KEY=true
  denied "readable in a job without an environment" &&
    assert "no API call" test ! -e gh.log
}
test_shared_key_rejected_for_stub_too() { setup; export IN_HARNESS=stub HAS_SHARED_KEY=true; denied "readable in a job without an environment"; }
test_bad_shared_key_flag() { setup; export HAS_SHARED_KEY=''; denied "Invalid HAS_SHARED_KEY"; }

# --- Caps: lowered only ------------------------------------------------------------
test_caps() {
  local name var max v
  for spec in requests_per_hour:IN_RPH:100 requests_per_minute:IN_RPM:30 max_turns:IN_TURNS:80 \
    harness_timeout_minutes:IN_TIMEOUT:20; do
    IFS=: read -r name var max <<<"${spec}"
    for v in "$((max + 1))" 0 -1 1.5 abc '' 10000 ' 5' $'5\n6'; do
      setup
      export "${var}=${v}"
      denied "Input ${name} must be an integer in 1..${max}" || { echo "    ${name}='${v}'"; return 1; }
    done
    setup
    export "${var}=1"
    allowed || return 1
    setup
    export "${var}=${max}"
    allowed || return 1
  done
}

# --- M2: api_base is an allowlist entry ---------------------------------------------
test_api_base_allowlist() {
  local v
  for v in https://1.2.3.4/v1 https://api.inference.cscs.ch:443/v1 https://user@api.inference.cscs.ch/v1 \
    https://evil.example.com/v1 https://api.inference.cscs.ch/v1/ https://api.inference.cscs.ch \
    http://api.inference.cscs.ch/v1 https://API.inference.cscs.ch/v1 'https://api.inference.cscs.ch/v1 ' \
    https://api.inference.cscs.ch.evil.example/v1 $'https://api.inference.cscs.ch/v1\nhttps://x' \
    '#https://api.inference.cscs.ch/v1' https://localhost/v1 https://[::1]/v1; do
    setup
    export IN_API_BASE=${v}
    denied "is not an allowed inference endpoint" || { echo "    api_base='${v}'"; return 1; }
  done
}
test_api_base_empty_means_default() { setup; export IN_API_BASE=''; allowed && assert "default" test "$(out api_base)" = "${CSCS}"; }

# --- S5: paths, label, environment, project --------------------------------------------
test_rule_file_paths() {
  local v
  for v in '../etc/passwd' 'a..b.md' 'docs/../AGENTS.md' '/etc/passwd' 'a//b.md' './AGENTS.md' 'a/./b.md' \
    ':(glob)x' '-rf' 'a b.md' 'docs/*.md' 'docs/' "$(printf 'x%.0s' {1..201})"; do
    setup
    export IN_RULES=${v}
    denied "Invalid entry in rule_files" || { echo "    rule_files='${v}'"; return 1; }
  done
}
test_rule_files_skip_comments_and_blanks() {
  setup
  export IN_RULES=$'# a comment\n\n  AGENTS.md  \nsrc/pkg/AGENTS.md'
  allowed
}
test_instruction_patterns() {
  setup
  export IN_PATTERNS=$'docs/style/\n*.mdc\n# comment'
  allowed || return 1
  local v
  for v in 'docs/**/../x' 'docs//' '/docs/' 'a/./b/' '../x/'; do
    setup
    export IN_PATTERNS=${v}
    denied "Invalid entry in instruction_files" || { echo "    instruction_files='${v}'"; return 1; }
  done
}
test_addendum_paths() {
  local v
  for v in '#../../../../user' '# comment' 'a..b.md' '../x.md' '/x.md' 'a?ref=main' 'x%2f.md'; do
    setup
    export IN_ADDENDUM=${v}
    denied "Invalid entry in prompt_addendum" || { echo "    prompt_addendum='${v}'"; return 1; }
  done
}
test_addendum_two_lines() { setup; export IN_ADDENDUM=$'a.md\nb.md'; denied "takes one path"; }
test_addendum_empty_and_padded() {
  setup
  export IN_ADDENDUM=''
  allowed || return 1
  setup
  export IN_ADDENDUM=$'  .github/agent-review/prompt-addendum.md\n'
  allowed
}
test_label() {
  local v
  for v in '' ' agent-review' $'agent-review\nx' 'a"b' 'a$(id)' "$(printf 'x%.0s' {1..51})"; do
    setup
    export IN_LABEL=${v}
    denied "Invalid label" || { echo "    label='${v}'"; return 1; }
  done
  setup
  export IN_LABEL='needs review: agent'
  allowed
}
test_environment() { setup; export IN_ENVIRONMENT='-x'; denied "Invalid environment"; }
test_project() { setup; export IN_PROJECT='a;b'; denied "Invalid project name"; }
test_dry_run_flag() { setup; export IN_DRY_RUN=yes; denied "Invalid dry-run flag"; }

# --- Events --------------------------------------------------------------------------
test_prt_labeled_checks_the_labeller() {
  setup_prt labeled
  allowed &&
    assert "labeller checked" grep -qF "collaborators/sender/permission" gh.log &&
    assert "event's PR" test "$(out pr)" = 12 &&
    assert "event's head" test "$(out head_sha)" = "${HEAD}" &&
    assert "PR not resolved again" test "$(grep -c '/pulls/' gh.log)" -eq 0
}
test_prt_stub_is_allowed() { setup_prt labeled; export IN_HARNESS=stub; allowed && assert "keyless" test "$(out keyed)" = false; }
test_prt_other_label_is_skipped() { setup_prt labeled; export EVENT_LABEL=bug; skipped "Label 'bug' does not request a review"; }
test_prt_automatic_actions_check_the_author() {
  local a
  for a in opened reopened ready_for_review; do
    setup_prt "${a}"
    allowed || return 1
    assert "author checked (${a})" grep -qF "collaborators/author/permission" gh.log || return 1
    rm -f gh.log
  done
}
test_prt_draft_is_skipped() { setup_prt opened; export EVENT_DRAFT=true; skipped "Draft pull request"; }
test_prt_other_actions_are_skipped() {
  local a
  for a in synchronize edited unlabeled assigned closed review_requested ''; do
    setup_prt "${a}"
    skipped "does not start a review" || { echo "    action='${a}'"; return 1; }
    assert "no API call (${a})" test ! -e gh.log || return 1
  done
}
test_prt_rejects_test_repository() {
  setup_prt labeled
  export IN_HARNESS=stub REPO=${CENTRAL} IN_TEST_REPO=GridTools/gt4py
  denied "not supported under 'pull_request_target'"
}
test_writer_levels() {
  local p
  for p in admin write; do
    setup
    export FAKE_PERM=${p}
    allowed || return 1
  done
  for p in read triage none maintain ''; do
    setup
    export FAKE_PERM=${p}
    skipped "has '${p}' permission; skipping" || { echo "    permission='${p}'"; return 1; }
  done
}
test_api_failure_is_an_error() { setup; export FAKE_GH_FAIL=1; denied "Could not resolve pull request"; }
test_permission_api_failure_is_an_error() { setup_prt labeled; export FAKE_GH_FAIL=1; denied "Could not resolve repository permission"; }
test_dispatch_invalid_pr() { setup; export IN_PR='7; id'; denied "Invalid pull request number"; }
test_bad_head_sha_from_api() { setup; export FAKE_HEAD=main; denied "Invalid head sha"; }

# Caller test mode: 'pull_request', stub only, dry run from the event name.
test_pull_request_caller_stub() {
  setup
  export EVENT=pull_request IN_HARNESS=stub IN_PR='' EVENT_PR=12 IN_DRY_RUN=false
  allowed &&
    assert "forced dry run" test "$(out dry_run)" = true &&
    assert "keyless" test "$(out keyed)" = false &&
    assert "event's PR" test "$(out pr)" = 12 &&
    assert "no permission check" test "$(grep -c collaborators gh.log)" -eq 0 &&
    assert "test-mode notice" grep -qF "Caller test mode" log &&
    assert "PR content notice" grep -qF "are pull request content here" log
}
test_pull_request_keyed_is_rejected() {
  local h
  for h in opencode pi; do
    setup
    export EVENT=pull_request IN_HARNESS=${h} EVENT_PR=12
    denied "runs the 'stub' harness only" || return 1
    # Also from the central repository itself.
    setup
    export EVENT=pull_request IN_HARNESS=${h} REPO=${CENTRAL} TRUSTED_REPO=${CENTRAL}
    denied "runs the 'stub' harness only" || return 1
  done
}
test_pull_request_test_repository_from_caller() {
  setup
  export EVENT=pull_request IN_HARNESS=stub IN_TEST_REPO=GridTools/gt4py
  denied "is for the self-test of ${CENTRAL}"
}
test_push_and_schedule_are_self_test_only() {
  local e
  for e in push schedule; do
    setup
    export EVENT=${e} IN_HARNESS=stub
    denied "Event '${e}' is for the self-test of ${CENTRAL} only" || return 1
    setup
    export EVENT=${e} IN_HARNESS=opencode REPO=${CENTRAL}
    denied "runs the 'stub' harness only" || return 1
    setup
    export EVENT=${e} IN_HARNESS=stub REPO=${CENTRAL} IN_TEST_REPO=GridTools/gt4py IN_PR=2983
    allowed || return 1
    assert "forced dry run (${e})" test "$(out dry_run)" = true || return 1
    assert "source repo (${e})" test "$(out source_repo)" = GridTools/gt4py || return 1
    assert "resolved in the test repository" grep -qF "repos/GridTools/gt4py/pulls/2983" gh.log || return 1
  done
}
test_self_test_pull_request_has_no_caller_notice() {
  setup
  export EVENT=pull_request IN_HARNESS=stub REPO=${CENTRAL} IN_TEST_REPO=GridTools/gt4py IN_PR=2983
  allowed && assert "no caller notice" test "$(grep -c 'Caller test mode' log)" -eq 0
}
test_other_events_are_rejected() {
  local e
  for e in issue_comment pull_request_review workflow_run merge_group ''; do
    setup
    export EVENT=${e} IN_HARNESS=stub
    denied "Unexpected event" || { echo "    event='${e}'"; return 1; }
  done
}
test_test_repository_from_another_repository() {
  setup
  export IN_HARNESS=stub IN_TEST_REPO=GridTools/gt4py
  denied "Input test_repository is for the self-test"
}
test_test_repository_with_keyed_harness_in_central() {
  setup
  export REPO=${CENTRAL} IN_TEST_REPO=GridTools/gt4py
  denied "Input test_repository is for the self-test"
}
test_test_repository_dispatch_forces_dry_run() {
  setup
  export REPO=${CENTRAL} IN_HARNESS=stub IN_TEST_REPO=GridTools/gt4py IN_PR=2983
  allowed && assert "forced dry run" test "$(out dry_run)" = true
}
test_invalid_trusted_location() {
  setup
  export TRUSTED_SHA=''
  denied "No job.workflow_sha" || return 1
  setup
  export TRUSTED_REPO='a/b/c'
  denied "No job.workflow_repository"
}
test_errors_link_existing_docs_anchor() {
  setup
  export IN_HARNESS=x
  denied "docs/agent-review.md#inputs" &&
    assert "the anchor exists" grep -q '^### Inputs$' "${AR_DIR}/../docs/agent-review.md"
}

for t in $(declare -F | awk '$3 ~ /^test_/ {print $3}'); do run_test "$t"; done
summary
