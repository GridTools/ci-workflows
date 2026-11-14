#!/usr/bin/env bash
# Unit tests for bin/report-outcome: 0, 1 and several existing failure
# comments, for success and failure, and the job/step outcome table. Offline:
# 'gh' is a fake on PATH that keeps the PR's comments in a file and applies
# the script's --jq filter with jq.
#
# Run: bash pr-agent/tests/test-report-outcome.sh

set -uo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
PA_DIR=$(cd "$(dirname "$0")/.." && pwd)
MARKER='<!-- pr-agent-run-failed -->'

setup() {
  mkdir -p fakebin
  cat >fakebin/gh <<'EOS'
#!/usr/bin/env bash
# gh api [-X METHOD] PATH [--paginate] [--jq FILTER] [-f body=...] [--silent]
shift
method=GET path='' jq_filter='' body=''
while [ $# -gt 0 ]; do
  case "$1" in
    -X) method=$2; shift ;;
    --jq) jq_filter=$2; shift ;;
    -f) body=${2#body=}; shift ;;
    --paginate | --silent) ;;
    *) path=$1 ;;
  esac
  shift
done
# 'gh api PATH -f ...' without -X is a POST.
[ -z "${body}" ] || [ "${method}" != GET ] || method=POST
echo "${method} ${path}" >>"${TMP}/calls"
c="${TMP}/comments.json"
case "${method} ${path}" in
  "GET repos/example-org/example/issues/12/comments") jq -r "${jq_filter}" "${c}" ;;
  "DELETE repos/example-org/example/issues/comments/"*)
    id=${path##*/}; jq --argjson id "${id}" 'map(select(.id != $id))' "${c}" >"${c}.new" && mv "${c}.new" "${c}" ;;
  "PATCH repos/example-org/example/issues/comments/"*)
    id=${path##*/}; jq --argjson id "${id}" --arg b "${body}" 'map(if .id == $id then .body = $b else . end)' "${c}" >"${c}.new" && mv "${c}.new" "${c}" ;;
  "POST repos/example-org/example/issues/12/comments")
    jq --arg b "${body}" '. + [{id: 999, user: {login: "github-actions[bot]"}, body: $b}]' "${c}" >"${c}.new" && mv "${c}.new" "${c}" ;;
  *) echo "unexpected call: ${method} ${path}" >&2; exit 1 ;;
esac
EOS
  chmod +x fakebin/gh
  export PATH="${TMP}/fakebin:${PATH}"
  export REPO=example-org/example PR=12 JOB_RESULT=success OUTCOME=failure
  export RUN_URL=https://github.com/example-org/example/actions/runs/1 DOCS_URL=''
  # Unrelated comments: a human's, and the bot's own review without the marker.
  jq -n --arg m "${MARKER}" '[
    {id: 1, user: {login: "someone"}, body: ($m + " quoted by a human")},
    {id: 2, user: {login: "github-actions[bot]"}, body: "## PR Reviewer Guide"}]' >comments.json
}
add_failure_comments() { # ids...
  local id
  for id in "$@"; do
    jq --argjson id "${id}" --arg m "${MARKER}" '. + [{id: $id, user: {login: "github-actions[bot]"}, body: ($m + "\nold")}]' \
      comments.json >c.new && mv c.new comments.json
  done
}
run() { bash "${PA_DIR}/bin/report-outcome" >log 2>&1 || { cat log; return 1; }; }
failure_ids() { jq -r --arg m "${MARKER}" '[.[] | select(.user.login == "github-actions[bot]" and (.body | startswith($m))) | .id] | join(" ")' comments.json; }
unrelated_kept() { assert "unrelated comments kept" test "$(jq -r '[.[] | select(.id == 1 or .id == 2)] | length' comments.json)" = 2; }

test_failure_no_existing() {
  setup
  run && assert "one new comment" test "$(failure_ids)" = 999 && unrelated_kept &&
    assert "links the run" grep -qF "([log](${RUN_URL}), [docs](https://github.com/GridTools/ci-workflows/blob/main/docs/pr-agent.md))" comments.json &&
    assert "warning" grep -qF "::warning title=PR Agent failed::" log
}
test_failure_one_existing() {
  setup; add_failure_comments 10
  run && assert "updated in place" test "$(failure_ids)" = 10 && assert "new text" grep -qF "could not complete" comments.json &&
    assert "no POST" bash -c "! grep -q '^POST' calls" && unrelated_kept
}
test_failure_several_existing() {
  setup; add_failure_comments 10 11 12
  run && assert "only the newest kept" test "$(failure_ids)" = 12 && unrelated_kept
}
setup_success() { setup; export OUTCOME=success; }
test_success_none() {
  setup_success
  run && assert "nothing posted" test -z "$(failure_ids)" && assert "only a GET" test "$(cat calls)" = "GET repos/example-org/example/issues/12/comments"
}
test_success_one() { setup_success; add_failure_comments 10; run && assert "deleted" test -z "$(failure_ids)" && unrelated_kept; }
test_success_several() { setup_success; add_failure_comments 10 11 12; run && assert "all deleted" test -z "$(failure_ids)" && unrelated_kept; }
test_success_after_failed_job_setup() {
  # A success outcome wins over the job result.
  setup_success; export JOB_RESULT=failure; add_failure_comments 10; run && assert "deleted" test -z "$(failure_ids)"
}
test_job_failure_without_outcome() {
  # Environment denial, image pull failure or missing key: no step outcome.
  setup; export JOB_RESULT=failure OUTCOME=''
  run && assert "reported" test "$(failure_ids)" = 999
}
test_cancelled_reports_nothing() {
  setup; export JOB_RESULT=cancelled OUTCOME=''; add_failure_comments 10
  run && assert "untouched" test "$(failure_ids)" = 10 && assert "no API call" test ! -e calls && assert "notice" grep -qF "Nothing to report" log
}
test_step_cancelled_reports_nothing() { setup; export OUTCOME=cancelled; run && assert "no API call" test ! -e calls; }
test_caller_docs_linked() { setup; export DOCS_URL=https://example.org/doc; run && assert "caller docs" grep -qF "[docs](https://example.org/doc)" comments.json; }
test_invalid_pr() { setup; export PR='12/../1'; if bash "${PA_DIR}/bin/report-outcome" >log 2>&1; then return 1; fi; assert "error" grep -qF "Invalid pull request number" log; }

for t in $(declare -F | awk '{print $3}' | grep '^test_'); do run_test "${t}"; done
summary
