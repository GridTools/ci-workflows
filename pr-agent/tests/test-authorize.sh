#!/usr/bin/env bash
# Unit tests for bin/authorize: inputs, the event matrix and the writer
# check. Offline: 'gh' is a fake on
# PATH that answers the two API calls the script makes.
#
# Run: bash pr-agent/tests/test-authorize.sh

set -uo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

CENTRAL=GridTools/ci-workflows
CALLER=example-org/example
CSCS=https://api.inference.cscs.ch/v1
HEAD=$(printf 'a%.0s' {1..40})
WF_SHA=$(printf 'c%.0s' {1..40})
CALLER_SHA=$(printf 'd%.0s' {1..40})

# A valid automatic 'pull_request_target' run of a caller; tests change what
# they need.
setup() {
  mkdir -p fakebin
  cat >fakebin/gh <<'EOS'
#!/usr/bin/env bash
echo "gh $*" >>"${TMP}/gh.log"
[ -z "${FAKE_GH_FAIL:-}" ] || { echo "HTTP 502" >&2; exit 1; }
case "$2" in
  */pulls/*) echo "${FAKE_HEAD}" ;;
  */collaborators/*/permission) echo "${FAKE_PERM}" ;;
  *) echo "unexpected call: $*" >&2; exit 1 ;;
esac
EOS
  chmod +x fakebin/gh
  export PATH="${TMP}/fakebin:${PATH}" GITHUB_OUTPUT="${TMP}/out"
  export FAKE_HEAD=${HEAD} FAKE_PERM=write
  export REPO=${CALLER} EVENT=pull_request_target ACTION=opened
  export PR_NUMBER=12 PR_AUTHOR=author PR_DRAFT=false PR_HEAD_REPO=someone/example
  export ISSUE_NUMBER='' ISSUE_IS_PR=false COMMENTER=''
  export IN_API_BASE=${CSCS} IN_ENVIRONMENT=pr-agent IN_DOCS_URL='' IN_DRY_RUN=false IN_TEST_PR=''
  export TRUSTED_REPO=${CENTRAL} TRUSTED_SHA=${WF_SHA} CALLER_WF_SHA=${CALLER_SHA} HAS_SHARED_KEY=false
}
# An 'issue_comment' on a pull request.
setup_comment() {
  setup
  export EVENT=issue_comment ACTION=created PR_NUMBER='' PR_AUTHOR='' PR_DRAFT='' PR_HEAD_REPO=''
  export ISSUE_NUMBER=34 ISSUE_IS_PR=true COMMENTER=commenter
}

run() { bash "${PA_DIR}/bin/authorize" >log 2>&1; }
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
skipped() { # expected notice: exit 0, allowed=false, result=skipped
  run || { cat log; return 1; }
  assert "allowed=false" test "$(out allowed)" = false &&
    assert "result=skipped" test "$(out result)" = skipped &&
    assert "says '$1'" grep -qF -- "$1" log
}
PA_DIR=$(cd "$(dirname "$0")/.." && pwd)

# --- Automatic runs ---------------------------------------------------------------
test_prt_outputs() {
  setup
  allowed &&
    assert "result" test "$(out result)" = incomplete &&
    assert "not a dry run" test "$(out dry_run)" = false &&
    assert "pr" test "$(out pr)" = 12 &&
    assert "api base" test "$(out api_base)" = "${CSCS}" &&
    assert "trusted" test "$(out trusted_sha)" = "${WF_SHA}" &&
    assert "author checked" grep -qF "repos/${CALLER}/collaborators/author/permission" gh.log &&
    assert "self-location notice" grep -qF "PR Agent workflow ${CENTRAL}@${WF_SHA}; caller ${CALLER}@${CALLER_SHA}." log
}
test_prt_actions() {
  local a
  for a in reopened ready_for_review; do
    setup; export ACTION=${a}; allowed || return 1
  done
}
test_prt_other_action() { setup; export ACTION=synchronize; skipped "Action 'synchronize' of 'pull_request_target' does not start PR Agent" && assert "no API call" test ! -e gh.log; }
test_prt_draft() { setup; export PR_DRAFT=true; skipped "Draft pull request"; }
test_dry_run() {
  setup
  export IN_DRY_RUN=true
  allowed && assert "result" test "$(out result)" = dry-run && assert "dry run" test "$(out dry_run)" = true &&
    assert "writer still checked" test -e gh.log
}

# --- Writer check -----------------------------------------------------------------
test_permission_admin() { setup; export FAKE_PERM=admin; allowed; }
test_permission_write() { setup; export FAKE_PERM=write; allowed; }
test_permission_read() { setup; export FAKE_PERM=read; skipped "author has 'read' permission; skipping PR Agent."; }
test_permission_none() { setup; export FAKE_PERM=none; skipped "author has 'none' permission"; }
test_permission_triage_reads_as_read() { setup; export FAKE_PERM=triage; skipped "author has 'triage' permission"; }
test_permission_empty() { setup; export FAKE_PERM=''; skipped "author has '' permission"; }
test_api_error_fails_closed() { setup; export FAKE_GH_FAIL=1; denied "Could not resolve repository permission for 'author', denying: 'HTTP 502'."; }
test_invalid_actor() { setup; export PR_AUTHOR='a/../b'; denied "Invalid actor 'a/../b'."; }
test_bot_actor_format() { setup; export PR_AUTHOR='renovate[bot]'; allowed; }

# --- Comments ---------------------------------------------------------------------
# Commands and their arguments are PR Agent's to parse: 'authorize' never reads
# the comment, so the body is not even in its environment.
test_comment_review() {
  setup_comment
  allowed && assert "pr is the issue" test "$(out pr)" = 34 &&
    assert "commenter checked" grep -qF "collaborators/commenter/permission" gh.log
}
test_comment_body_is_not_read() {
  setup_comment
  export BODY='/generate_labels --pr_description.publish_labels=true'
  allowed && assert "no mention of the body" bash -c "! grep -qF 'generate_labels' log"
}
test_comment_edited() { setup_comment; export ACTION=edited; skipped "Action 'edited' of 'issue_comment'"; }
test_comment_on_issue() { setup_comment; export ISSUE_IS_PR=false; skipped "Comment on an issue"; }
test_comment_non_writer() { setup_comment; export FAKE_PERM=read; skipped "commenter has 'read' permission"; }
test_comment_api_error() { setup_comment; export FAKE_GH_FAIL=1; denied "Could not resolve repository permission for 'commenter'"; }

# --- Caller test mode ---------------------------------------------------------------
test_pull_request_same_repo() {
  setup
  export EVENT=pull_request PR_HEAD_REPO=${CALLER}
  allowed && assert "test-mode notice" grep -qF "Caller test mode" log
}
test_pull_request_fork_skipped() { setup; export EVENT=pull_request; skipped "Pull request from a fork ('someone/example')"; }
test_pull_request_fork_dry_run() { setup; export EVENT=pull_request IN_DRY_RUN=true; allowed && assert "dry run" test "$(out result)" = dry-run; }

# --- Other events and the self-test -------------------------------------------------
test_unexpected_event() { setup; export EVENT=workflow_dispatch; denied "Unexpected event 'workflow_dispatch'"; }
test_push_without_test_pr() { setup; export EVENT=push REPO=${CENTRAL}; denied "Unexpected event 'push'"; }
test_self_test() {
  setup
  export EVENT=push REPO=${CENTRAL} IN_TEST_PR=example-org/fixture#3
  allowed && assert "forced dry run" test "$(out result)" = dry-run && assert "pr" test "$(out pr)" = 3 &&
    assert "fixture resolved" grep -qF "repos/example-org/fixture/pulls/3" gh.log &&
    assert "no writer check" bash -c "! grep -q collaborators gh.log"
}
test_self_test_events() {
  local e
  for e in pull_request schedule workflow_dispatch; do
    setup; export EVENT=${e} REPO=${CENTRAL} IN_TEST_PR=example-org/fixture#3; allowed || return 1
  done
}
test_test_pr_from_caller() { setup; export IN_TEST_PR=example-org/fixture#3; denied "Input test_pr is for the self-test of ${CENTRAL} only."; }
test_test_pr_under_prt() { setup; export REPO=${CENTRAL} IN_TEST_PR=example-org/fixture#3; denied "Input test_pr is not supported under 'pull_request_target'."; }
test_test_pr_invalid() { setup; export EVENT=push REPO=${CENTRAL} IN_TEST_PR='example-org/fixture#0'; denied "Invalid test_pr"; }
test_test_pr_api_error() { setup; export EVENT=push REPO=${CENTRAL} IN_TEST_PR=example-org/fixture#1 FAKE_GH_FAIL=1; denied "Could not resolve pull request example-org/fixture#1"; }

# --- Static inputs --------------------------------------------------------------------
test_shared_key() { setup; export HAS_SHARED_KEY=true; denied "readable in a job without an environment" && assert "no API call" test ! -e gh.log; }
test_bad_shared_key_flag() { setup; export HAS_SHARED_KEY=''; denied "Invalid HAS_SHARED_KEY"; }
test_bad_dry_run() { setup; export IN_DRY_RUN=yes; denied "Invalid dry_run 'yes'."; }
test_bad_environment() { setup; export IN_ENVIRONMENT='pr agent'; denied "Invalid environment 'pr agent'."; }
test_api_base_default() { setup; export IN_API_BASE=''; allowed && assert "default" test "$(out api_base)" = "${CSCS}"; }
test_api_base_other() { setup; export IN_API_BASE=https://evil.example/v1; denied "Input api_base 'https://evil.example/v1' is not an allowed inference endpoint"; }
test_api_base_trailing_slash() { setup; export IN_API_BASE=${CSCS}/; denied "is not an allowed inference endpoint"; }
test_docs_url() {
  setup
  export IN_DOCS_URL=https://github.com/example-org/example/blob/main/docs/pr_agent.md#usage
  allowed && assert "output" test "$(out docs_url)" = "${IN_DOCS_URL}"
}
test_docs_url_in_errors() {
  setup
  export IN_DOCS_URL=https://example.org/doc IN_DRY_RUN=maybe
  denied "See https://github.com/GridTools/ci-workflows/blob/main/docs/pr-agent.md#inputs and https://example.org/doc."
}
test_docs_url_invalid() {
  local u
  for u in http://example.org 'https://example.org/a)b' 'https://example.org/a b' 'https://example.org/[x]' 'javascript:x'; do
    setup; export IN_DOCS_URL=${u}; denied "Invalid docs_url" || { echo "    url: '${u}'"; return 1; }
  done
}

for t in $(declare -F | awk '{print $3}' | grep '^test_'); do run_test "${t}"; done
summary
