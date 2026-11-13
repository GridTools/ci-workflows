#!/usr/bin/env bash
# The 'authorize' job of the agent review: validates the caller's inputs,
# decides whether this event may run a review and with which key mode, checks
# the actor's permission and pins the pull request's head and base sha.
#
# Usage: authorize   (configuration in the environment, see below)
#
# Runs on the host, before anything from the pull request is read. It needs
# 'gh' (with GH_TOKEN) for the permission check and to resolve a pull request
# number, and writes its outputs to $GITHUB_OUTPUT. It fails closed: an
# invalid input or an API failure is an error ('allowed=false', exit 1); a
# denied actor or an event that is not for us is a notice ('allowed=false',
# exit 0).
#
# Environment (all from the workflow; IN_* are the caller's inputs, which come
# from the caller's workflow file):
#   REPO EVENT ACTION AUTHOR SENDER ACTOR EVENT_PR EVENT_HEAD_SHA
#   EVENT_BASE_SHA EVENT_DRAFT EVENT_LABEL       github context, event payload
#   IN_PR IN_HARNESS IN_MODEL IN_DRY_RUN IN_LABEL IN_PROJECT IN_RULES
#   IN_PATTERNS IN_ADDENDUM IN_API_BASE IN_RPH IN_RPM IN_TURNS IN_TIMEOUT
#   IN_ENVIRONMENT IN_TEST_REPO                  the caller's inputs
#   TRUSTED_REPO TRUSTED_SHA                     job.workflow_repository/_sha
#   CALLER_WF_SHA                                the caller's github.workflow_sha
#   HAS_SHARED_KEY  'true' if 'secrets.inference_api_key' is non-empty in this
#                   job, which declares no environment
#
# Event and harness matrix ('keyed' harnesses call a model with the key):
#   pull_request_target  any harness; actions 'labeled' (with the configured
#                        label), 'opened', 'reopened', 'ready_for_review';
#                        writer check on the labeller or the author
#   workflow_dispatch    any harness; writer check on the actor
#   pull_request         'stub' only, forced dry run, no writer check: the
#                        caller test mode (any caller) and the self-test
#   push, schedule       'stub' only, forced dry run, no writer check; the
#                        self-test of the workflow's own repository only

set -euo pipefail

readonly DOCS="See https://github.com/GridTools/ci-workflows/blob/main/docs/agent-review.md#inputs."
TRUSTED_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)

: "${GITHUB_OUTPUT:?}"
for v in REPO EVENT ACTION AUTHOR SENDER ACTOR EVENT_PR EVENT_HEAD_SHA EVENT_BASE_SHA EVENT_DRAFT \
  EVENT_LABEL IN_PR IN_HARNESS IN_MODEL IN_DRY_RUN IN_LABEL IN_PROJECT IN_RULES IN_PATTERNS \
  IN_ADDENDUM IN_API_BASE IN_RPH IN_RPM IN_TURNS IN_TIMEOUT IN_ENVIRONMENT IN_TEST_REPO \
  TRUSTED_REPO TRUSTED_SHA CALLER_WF_SHA HAS_SHARED_KEY; do
  declare -g "${v}=${!v-}"
done

fail() {
  echo "allowed=false" >>"${GITHUB_OUTPUT}"
  echo "::error::$1 ${DOCS}"
  exit 1
}
skip() {
  echo "::notice::$1"
  echo "allowed=false" >>"${GITHUB_OUTPUT}"
  exit 0
}
resolve_pr() { # number
  [[ "$1" =~ ^[1-9][0-9]{0,9}$ ]] || fail "Invalid pull request number '$1'."
  local shas
  if ! shas=$(gh api "repos/${source_repo}/pulls/$1" --jq '[.head.sha, .base.sha] | @tsv' 2>&1); then
    fail "Could not resolve pull request ${source_repo}#$1: '${shas}'."
  fi
  read -r head_sha base_sha <<<"${shas}"
}
int_in() { # name value max
  if ! [[ "$2" =~ ^[1-9][0-9]{0,3}$ ]] || [ "$2" -gt "$3" ]; then
    fail "Input $1 must be an integer in 1..$3, got '$2'."
  fi
}
# One repository path or pattern: no absolute path, no '..' anywhere (as
# 'run-review' checks), no empty or '.' segment. A pattern may end in '/'
# (a directory, see instruction-files.txt); a path may not.
check_path() { # name entry plain|glob
  local re='^[A-Za-z0-9_.][A-Za-z0-9_./-]{0,199}$' p=$2
  if [ "$3" = glob ]; then
    re='^[A-Za-z0-9_.*][A-Za-z0-9_.*/-]{0,199}$'
    p=${p%/}
  fi
  [[ "$2" =~ ${re} ]] || fail "Invalid entry in $1: '$2'."
  case "/${p}/" in *..* | */./* | *//*) fail "Invalid entry in $1: '$2'." ;; esac
}
# A list, one entry per line; blank lines and '#' comments are skipped, as
# 'run-review' reads these lists.
check_list() { # name list plain|glob
  local line
  while IFS= read -r line; do
    line=${line#"${line%%[![:space:]]*}"}
    line=${line%"${line##*[![:space:]]}"}
    case "${line}" in '' | '#'*) continue ;; esac
    check_path "$1" "${line}" "$3"
  done <<<"$2"
}

# --- This workflow's own commit, from which every trusted file is read ------
[[ "${TRUSTED_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "No job.workflow_repository ('${TRUSTED_REPO}')."
[[ "${TRUSTED_SHA}" =~ ^[0-9a-f]{40}$ ]] || fail "No job.workflow_sha ('${TRUSTED_SHA}')."
[[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "Invalid repository '${REPO}'."
echo "::notice::Agent review workflow ${TRUSTED_REPO}@${TRUSTED_SHA}; caller ${REPO}@${CALLER_WF_SHA}."

# --- Harness and key mode -----------------------------------------------------
# The allowlist decides whether the run is keyed: only keyed runs get the
# job with the caller's environment, and so the key.
harness=${IN_HARNESS}
case "${harness}" in
  stub) keyed=false ;;
  opencode | pi) keyed=true ;;
  '') fail "No harness given (input 'harness')." ;;
  *) fail "Unknown harness '${harness}'." ;;
esac

# The key must be an environment secret: here, in a job without an
# environment, the caller's mapping yields a repository or organization
# secret of that name, which every job of this workflow would then receive.
case "${HAS_SHARED_KEY}" in
  false) ;;
  true) fail "Secret 'inference_api_key' is readable in a job without an environment: the caller maps a repository or organization secret. Map it only from the environment's own secret, and delete the shared one." ;;
  *) fail "Invalid HAS_SHARED_KEY '${HAS_SHARED_KEY}'." ;;
esac

# --- Static inputs ------------------------------------------------------------
# Validated before any API call, so a misconfigured caller fails visibly.
[[ "${IN_LABEL}" =~ ^[A-Za-z0-9][A-Za-z0-9\ ._:/-]{0,49}$ ]] || fail "Invalid label '${IN_LABEL}'."
case "${IN_DRY_RUN}" in true | false) ;; *) fail "Invalid dry-run flag '${IN_DRY_RUN}'." ;; esac
# The stub calls no model. The others have no default model on purpose.
if [ "${keyed}" = false ]; then
  model=none
else
  model=${IN_MODEL}
  [ -n "${model}" ] || fail "No model given for harness '${harness}' (input 'model')."
  [[ "${model}" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$ ]] || fail "Invalid model id '${model}'."
fi
project=${IN_PROJECT:-${REPO#*/}}
[[ "${project}" =~ ^[A-Za-z0-9][A-Za-z0-9._\ -]{0,63}$ ]] || fail "Invalid project name '${project}'."
check_list rule_files "${IN_RULES}" plain
check_list instruction_files "${IN_PATTERNS}" glob
# One path, no comment syntax: the value becomes part of an API path.
if [ -n "${IN_ADDENDUM}" ]; then
  [ "$(grep -c . <<<"${IN_ADDENDUM}")" -le 1 ] || fail "Input prompt_addendum takes one path."
  addendum=${IN_ADDENDUM#"${IN_ADDENDUM%%[![:space:]]*}"}
  addendum=${addendum%"${addendum##*[![:space:]]}"}
  check_path prompt_addendum "${addendum}" plain
fi
# Where the key is sent: an exact entry of the central allowlist.
api_base=${IN_API_BASE}
mapfile -t api_bases < <(grep -v -e '^#' -e '^$' "${TRUSTED_ROOT}/api-bases.txt")
[ "${#api_bases[@]}" -gt 0 ] || fail "No allowed inference endpoint in api-bases.txt."
[ -n "${api_base}" ] || api_base=${api_bases[0]}
allowed_base=false
for b in "${api_bases[@]}"; do [ "${api_base}" != "${b}" ] || allowed_base=true; done
[ "${allowed_base}" = true ] || fail "Input api_base '${api_base}' is not an allowed inference endpoint (agent-review/api-bases.txt)."
[[ "${IN_ENVIRONMENT}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] || fail "Invalid environment '${IN_ENVIRONMENT}'."
# Ceilings: a caller can lower the caps, never raise them. The review job
# also clamps them to agent-review/awf.yaml.
int_in requests_per_hour "${IN_RPH}" 100
int_in requests_per_minute "${IN_RPM}" 30
int_in max_turns "${IN_TURNS}" 80
int_in harness_timeout_minutes "${IN_TIMEOUT}" 20

# --- Event ----------------------------------------------------------------------
# The self-test: this repository calling its own workflow with the stub.
self_test=false
[ "${TRUSTED_REPO}" != "${REPO}" ] || [ "${keyed}" = true ] || self_test=true

# The repository whose pull request is reviewed: the caller's, or (self-test
# only) another public repository, read-only.
source_repo=${REPO}
if [ -n "${IN_TEST_REPO}" ]; then
  [ "${self_test}" = true ] || fail "Input test_repository is for the self-test of ${TRUSTED_REPO} with the 'stub' harness only."
  [ "${EVENT}" != pull_request_target ] || fail "Input test_repository is not supported under 'pull_request_target'."
  [[ "${IN_TEST_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "Invalid test_repository '${IN_TEST_REPO}'."
  source_repo=${IN_TEST_REPO}
fi

dry_run=${IN_DRY_RUN}
check_writer=true
case "${EVENT}" in
  pull_request_target)
    # Only these actions; a caller listing other 'types' (or none, which
    # means every push to a PR) gets no keyed run from them.
    case "${ACTION}" in
      labeled)
        [ "${EVENT_LABEL}" = "${IN_LABEL}" ] || skip "Label '${EVENT_LABEL}' does not request a review."
        actor=${SENDER}
        ;;
      opened | reopened | ready_for_review)
        [ "${EVENT_DRAFT}" != true ] || skip "Draft pull request; add the '${IN_LABEL}' label to request a review."
        actor=${AUTHOR}
        ;;
      *) skip "Action '${ACTION}' of 'pull_request_target' does not start a review (allowed: labeled, opened, reopened, ready_for_review)." ;;
    esac
    pr=${EVENT_PR}
    head_sha=${EVENT_HEAD_SHA}
    base_sha=${EVENT_BASE_SHA}
    ;;
  workflow_dispatch)
    pr=${IN_PR}
    resolve_pr "${pr}"
    actor=${ACTOR}
    ;;
  pull_request | push | schedule)
    # Nothing to spend and nothing to post, so no writer check (fork and
    # Dependabot PRs can run it). The dry run follows from the event name.
    [ "${keyed}" = false ] || fail "Event '${EVENT}' runs the 'stub' harness only, not '${harness}'."
    if [ "${EVENT}" != pull_request ]; then
      [ "${self_test}" = true ] || fail "Event '${EVENT}' is for the self-test of ${TRUSTED_REPO} only."
    elif [ "${self_test}" = false ]; then
      echo "::notice::Caller test mode (event 'pull_request', harness 'stub', dry run): the caller's workflow file, inputs, rule files and prompt addendum are pull request content here. This run proves the plumbing only, never the reset of the rule files."
    fi
    pr=${IN_PR:-${EVENT_PR}}
    resolve_pr "${pr}"
    actor=${ACTOR}
    check_writer=false
    dry_run=true
    ;;
  *) fail "Unexpected event '${EVENT}'." ;;
esac
# Never comment on a pull request of the caller that merely has the same
# number as the reviewed one.
[ "${source_repo}" = "${REPO}" ] || dry_run=true

if [ "${check_writer}" = true ]; then
  [[ "${actor}" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$ ]] || fail "Invalid actor '${actor}'."
  if ! level=$(gh api "repos/${REPO}/collaborators/${actor}/permission" --jq '.permission' 2>&1); then
    fail "Could not resolve repository permission for '${actor}', denying: '${level}'."
  fi
  # 'permission' reports the 'maintain' role as 'write', and 'triage' as
  # 'read'.
  case "${level}" in
    admin | write) echo "::notice::${actor} has '${level}' permission; running the agent review." ;;
    *) skip "${actor} has '${level}' permission; skipping the agent review." ;;
  esac
else
  echo "::notice::Event '${EVENT}' with harness 'stub': forced dry run, no permission check."
fi

[[ "${pr}" =~ ^[1-9][0-9]{0,9}$ ]] || fail "Invalid pull request number '${pr}'."
[[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] || fail "Invalid head sha '${head_sha}'."
[[ "${base_sha}" =~ ^[0-9a-f]{40}$ ]] || fail "Invalid base sha '${base_sha}'."

# Distinguishes this call's artifacts from another call's in the same run.
call_id=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')

{
  echo "allowed=true"
  echo "keyed=${keyed}"
  echo "pr=${pr}"
  echo "head_sha=${head_sha}"
  echo "base_sha=${base_sha}"
  echo "harness=${harness}"
  echo "model=${model}"
  echo "dry_run=${dry_run}"
  echo "project=${project}"
  echo "api_base=${api_base}"
  echo "source_repo=${source_repo}"
  echo "call_id=${call_id}"
  echo "trusted_repo=${TRUSTED_REPO}"
  echo "trusted_sha=${TRUSTED_SHA}"
} >>"${GITHUB_OUTPUT}"
echo "::notice::Reviewing ${source_repo}#${pr} at ${head_sha} with harness '${harness}' (keyed ${keyed}), model '${model}', dry run '${dry_run}'."
