#!/usr/bin/env bash
# In-sandbox driver of the agent review; harness-agnostic.
#
# Usage: run-review [--phase fetch|review|all] <workspace>
#
# Runs INSIDE the AWF sandbox (see docs/agent-review.md),
# in two sandbox runs, so that the agent never has network access:
#   fetch   fetches the PR from github.com (the only allowed domain), with
#           every object it needs, prepares the checkout and renders the
#           prompt; no inference key, no harness;
#   review  runs the harness with no allowed domain (only the inference
#           listener) and collects its review.
# 'all' (the default) does both in one run, for local tests.
# Everything it fetches from the pull request is untrusted: git runs with
# hooks, global/system config, submodules and LFS disabled, and nothing from
# the checkout is executed here. The harness adapter is the only component
# that acts on the content, and it holds no secret.
#
# Inputs, written by the trusted host before the sandbox starts:
#   <workspace>/in/params.json  validated parameters (repo, pr, shas, harness, ...)
#   <workspace>/in/pr.json      raw GitHub API response for the PR (untrusted)
# Outputs:
#   <workspace>/out/review.md   the review, at most REVIEW_MAX_BYTES
#   <workspace>/out/meta.json   status of the run
#
# Exit codes: 0 review written (or, for 'fetch', the checkout prepared),
# 2 the harness ran but produced no review, 1 anything else.

set -euo pipefail
shopt -u patsub_replacement 2>/dev/null || true

# Leaves room for the comment header under GitHub's 65536-character limit and
# under the 64 KiB 'collect-result' cap on the host.
readonly REVIEW_MAX_BYTES=60000
readonly PR_BODY_MAX_BYTES=16384
readonly PR_TITLE_MAX_BYTES=512

die() {
  echo "run-review: $*" >&2
  exit 1
}

# Byte truncation can split a multi-byte character; 'iconv -c' drops it (and
# drops invalid bytes), exiting 1 when it does. Used by both phases, which run
# as separate processes.
clip() { head -c "$1" | { iconv -f UTF-8 -t UTF-8 -c 2>/dev/null || true; }; }

PHASE=all
if [ "${1:-}" = --phase ]; then
  PHASE=${2:-}
  shift 2 || die "Usage: run-review [--phase fetch|review|all] <workspace>"
fi
case "${PHASE}" in fetch | review | all) ;; *) die "Invalid phase '${PHASE}'." ;; esac
[ $# -eq 1 ] || die "Usage: run-review [--phase fetch|review|all] <workspace>"
WS=$1
case "${WS}" in /*) ;; *) die "The workspace must be an absolute path, got '${WS}'." ;; esac

TRUSTED_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
IN_DIR="${WS}/in"
REPO_DIR="${WS}/repo"
CONTEXT_DIR="${REPO_DIR}/.agent-review-context"
HARNESS_OUT="${WS}/harness-out"
RESULT_DIR="${WS}/out"
SCRATCH_HOME="${WS}/scratch-home"
SCRATCH_TMP="${WS}/scratch-tmp"

# --- Parameters -----------------------------------------------------------
param() { jq -er --arg k "$1" '.[$k] | tostring' "${IN_DIR}/params.json"; }

REPO=$(param repo)
PR_NUMBER=$(param pr)
HEAD_SHA=$(param head_sha)
BASE_SHA=$(param base_sha)
HARNESS=$(param harness)
MODEL=$(param model)
HARNESS_TIMEOUT_MINUTES=$(param harness_timeout_minutes)
STUB_PADDING_KIB=$(jq -r '.stub_padding_kib // 0 | tostring' "${IN_DIR}/params.json")
PROJECT=$(jq -r '.project // "" | tostring' "${IN_DIR}/params.json")
[ -n "${PROJECT}" ] || PROJECT=${REPO#*/}

# Validated in the 'authorize' job already; checked again because these become
# paths, URLs and command arguments.
[[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "Invalid repo '${REPO}'."
[[ "${PR_NUMBER}" =~ ^[1-9][0-9]{0,9}$ ]] || die "Invalid PR number '${PR_NUMBER}'."
[[ "${HEAD_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "Invalid head sha '${HEAD_SHA}'."
[[ "${BASE_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "Invalid base sha '${BASE_SHA}'."
[[ "${HARNESS}" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || die "Invalid harness '${HARNESS}'."
[[ "${MODEL}" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$ ]] || die "Invalid model '${MODEL}'."
[[ "${HARNESS_TIMEOUT_MINUTES}" =~ ^[1-9][0-9]{0,2}$ ]] || die "Invalid timeout '${HARNESS_TIMEOUT_MINUTES}'."
[[ "${STUB_PADDING_KIB}" =~ ^[0-9]{1,5}$ ]] || die "Invalid stub padding '${STUB_PADDING_KIB}'."
[[ "${PROJECT}" =~ ^[A-Za-z0-9][A-Za-z0-9._\ -]{0,63}$ ]] || die "Invalid project '${PROJECT}'."

# Caller configuration (trusted: the caller's validated inputs and its
# addendum file at the caller's workflow commit), next to this script's own
# files. Missing files mean "not configured".
CONFIG_DIR="${TRUSTED_ROOT}/config"
read_list() { # file -> non-comment, trimmed lines on stdout
  [ -f "$1" ] || return 0
  sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$1" | grep -v -e '^$' -e '^#' || true
}
check_entry() { # entry
  case "$1" in /* | :* | *..*) die "Invalid configured path or pattern: '$1'." ;; esac
}
mapfile -t RULE_FILES < <(read_list "${CONFIG_DIR}/rule-files.txt")
for f in "${RULE_FILES[@]}"; do check_entry "${f}"; done
# Reset patterns: the central list (every harness's files), the caller's
# extra patterns, and every rule file the prompt tells the agent to follow.
mapfile -t RESET_PATTERNS < <(
  read_list "${TRUSTED_ROOT}/instruction-files.txt"
  read_list "${CONFIG_DIR}/extra-instruction-files.txt"
  printf '%s\n' "${RULE_FILES[@]}"
)
for f in "${RESET_PATTERNS[@]}"; do [ -z "${f}" ] || check_entry "${f}"; done

# True if repository path $1 is reset by some pattern (same semantics as the
# pathspecs below: 'E' matches E at any depth, 'D/' anything under D).
covered() {
  local path=$1 entry
  for entry in "${RESET_PATTERNS[@]}"; do
    [ -n "${entry}" ] || continue
    if [[ "${entry}" == */ ]]; then
      case "/${path}/" in */"${entry%/}"/*) return 0 ;; esac
    else
      # shellcheck disable=SC2053,SC2254 # entries may be globs
      [[ "${path}" == ${entry} || "${path}" == */${entry} ]] && return 0
    fi
  done
  return 1
}
# Fail closed: a file the addendum names as a rule must be reset too, or a PR
# could rewrite it and have the agent follow the PR's version. This early
# check uses bash matching, where '*' also crosses '/', unlike the git
# pathspecs of the reset; the post-condition after the reset
# ('verify_followed_files') is what enforces it.
ADDENDUM_FILE="${CONFIG_DIR}/prompt-addendum.md"
ADDENDUM_NAMED=()
if [ -s "${ADDENDUM_FILE}" ]; then
  # The addendum is substituted into the prompt before the untrusted PR
  # title and body; a placeholder in it would be expanded with them, outside
  # their delimiters.
  if grep -qF '{{' "${ADDENDUM_FILE}"; then
    die "The prompt addendum contains '{{'; placeholders are not allowed there."
  fi
  mapfile -t ADDENDUM_NAMED < <(grep -oE '`[^`[:space:]]+\.(md|mdc|txt|toml|ya?ml|json|jsonc)`' "${ADDENDUM_FILE}" | tr -d '`' | sort -u)
  for named in "${ADDENDUM_NAMED[@]}"; do
    # A path from the repository root, which the post-condition can check.
    case "/${named}/" in */../* | */./* | *//* | *[*?[]*)
      die "The prompt addendum names '${named}', which is not a plain repository path." ;;
    esac
    covered "${named}" || die "The prompt addendum names '${named}', which no reset pattern covers; add it to rule_files."
  done
fi

ADAPTER_DIR="${TRUSTED_ROOT}/harnesses/${HARNESS}"
HARNESS_PREFIX="${TRUSTED_ROOT}/harness/${HARNESS}"
[ -f "${ADAPTER_DIR}/run.sh" ] || die "No adapter for harness '${HARNESS}'."
[ -d "${HARNESS_PREFIX}" ] || die "Harness '${HARNESS}' is not installed at '${HARNESS_PREFIX}'."

write_meta() { # status harness_exit truncated review_bytes
  mkdir -p "${RESULT_DIR}"
  jq -n \
    --arg harness "${HARNESS}" --arg model "${MODEL}" --arg status "$1" \
    --argjson harness_exit "$2" --argjson truncated "$3" --argjson review_bytes "$4" \
    '{harness: $harness, model: $model, status: $status, harness_exit: $harness_exit,
      truncated: $truncated, review_bytes: $review_bytes}' >"${RESULT_DIR}/meta.json"
}

# --- Fetch the pull request ------------------------------------------------
# No user or system git config: the host's may enable LFS filters or other
# helpers. Hooks and fsmonitor are disabled on every call since '.git' could
# otherwise be influenced by repository content.
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0
export GIT_LFS_SKIP_SMUDGE=1 GIT_NO_REPLACE_OBJECTS=1
git_safe() {
  git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.symlinks=true \
    -c protocol.file.allow=never -c protocol.ext.allow=never \
    -c submodule.recurse=false -c diff.external= -c advice.detachedHead=false \
    -C "${REPO_DIR}" "$@"
}

PROMPT_FILE="${WS}/prompt.md"

# Post-condition of the reset: every file the prompt tells the agent to
# follow (each rule file, and each file the addendum names, as a path from
# the repository root) is exactly its base version, or absent in both the
# base and the checkout. Checked on the result rather than on the patterns,
# so a pattern that does not match what it seems to cannot let the PR's
# version through. No leading directory may be a symlink, so the path in the
# checkout is the path git wrote.
verify_followed_files() {
  local path entry mode type id rest dir
  for path in "${RULE_FILES[@]}" "${ADDENDUM_NAMED[@]}"; do
    [ -n "${path}" ] || continue
    case "/${path}/" in */../* | */./* | *//* | *[*?[]*) die "Invalid followed file '${path}': not a plain path." ;; esac
    dir=${path}
    while [[ "${dir}" == */* ]]; do
      dir=${dir%/*}
      [ ! -L "${REPO_DIR}/${dir}" ] || die "Followed file '${path}': '${dir}' is a symlink in the checkout."
    done
    entry=$(git_safe ls-tree --full-tree "${BASE_SHA}" -- "${path}")
    if [ -z "${entry}" ]; then
      if [ -e "${REPO_DIR}/${path}" ] || [ -L "${REPO_DIR}/${path}" ]; then
        die "Followed file '${path}' is not in the base commit but is in the checkout: the reset missed it."
      fi
      continue
    fi
    read -r mode type id rest <<<"${entry}"
    [ "${rest}" = "${path}" ] || die "Followed file '${path}': unexpected base entry '${entry}'."
    case "${mode}:${type}" in
      100644:blob | 100755:blob)
        [ -f "${REPO_DIR}/${path}" ] && [ ! -L "${REPO_DIR}/${path}" ] &&
          [ "$(git_safe hash-object --no-filters -- "${path}")" = "${id}" ] ||
          die "Followed file '${path}' is not its base version in the checkout: the reset missed it."
        ;;
      120000:blob)
        [ -L "${REPO_DIR}/${path}" ] && [ "$(readlink -- "${REPO_DIR}/${path}")" = "$(git_safe cat-file blob "${id}")" ] ||
          die "Followed file '${path}' is not its base version in the checkout: the reset missed it."
        ;;
      *) die "Followed file '${path}' is not a file in the base commit (mode ${mode})." ;;
    esac
  done
  echo "run-review: ${#RULE_FILES[@]} rule file(s) and ${#ADDENDUM_NAMED[@]} file(s) named by the addendum are their base versions."
}

fetch_phase() {
  rm -rf "${REPO_DIR}" "${PROMPT_FILE}" "${HARNESS_OUT}" "${RESULT_DIR}" "${SCRATCH_HOME}" "${SCRATCH_TMP}"
  mkdir -p "${REPO_DIR}" "${SCRATCH_HOME}" "${SCRATCH_TMP}"
  # Everything stays in the workspace, the only directory the sandbox may
  # write to ('filesystem.allowWrite').
  export HOME="${SCRATCH_HOME}" TMPDIR="${SCRATCH_TMP}"
  git_safe init -q
  git_safe remote add origin "https://github.com/${REPO}.git"

  # Fetch by sha so the reviewed commit is exactly the one authorized; never
  # fall back to the moving 'refs/pull/N/head'. Full objects, no partial clone:
  # the review phase has no network, so nothing can be fetched lazily there.
  git_safe fetch -q --no-tags --depth=50 origin "${HEAD_SHA}" "${BASE_SHA}"
  [ "$(git_safe rev-parse --verify "${HEAD_SHA}^{commit}")" = "${HEAD_SHA}" ] ||
    die "Fetched head does not match ${HEAD_SHA}."
  [ "$(git_safe rev-parse --verify "${BASE_SHA}^{commit}")" = "${BASE_SHA}" ] ||
    die "Fetched base does not match ${BASE_SHA}."

  # A three-dot diff needs the merge base, which a shallow fetch may lack.
  MERGE_BASE=""
  for deepen in 200 1000 5000; do
    if MERGE_BASE=$(git_safe merge-base "${BASE_SHA}" "${HEAD_SHA}" 2>/dev/null); then
      break
    fi
    git_safe fetch -q --no-tags --deepen="${deepen}" origin "${HEAD_SHA}" "${BASE_SHA}"
  done
  if [ -z "${MERGE_BASE}" ]; then
    git_safe fetch -q --no-tags --unshallow origin "${HEAD_SHA}" "${BASE_SHA}"
    MERGE_BASE=$(git_safe merge-base "${BASE_SHA}" "${HEAD_SHA}") || die "No merge base between base and head."
  fi

  git_safe checkout -q --detach "${HEAD_SHA}"

  # --- Context files -----------------------------------------------------------
  # The PR may itself contain '.agent-review-context'; git removes it safely
  # (it does not follow symlinks), then the directory is created fresh.
  git_safe rm -r -q -f --ignore-unmatch -- .agent-review-context
  rm -rf "${CONTEXT_DIR}"
  mkdir -p "${CONTEXT_DIR}/pr-instruction-files"
  printf '/.agent-review-context/\n' >>"${REPO_DIR}/.git/info/exclude"

  git_safe diff --no-ext-diff --no-textconv "${MERGE_BASE}" "${HEAD_SHA}" >"${CONTEXT_DIR}/diff.patch"
  git_safe diff --no-ext-diff --name-status "${MERGE_BASE}" "${HEAD_SHA}" >"${CONTEXT_DIR}/changed-files.txt"

  # --- Reset agent instruction files to the base version -----------------------
  # Every pattern applies at any depth (see instruction-files.txt), and
  # case-insensitively, since a harness may not care about case either.
  SPECS=()
  for pattern in "${RESET_PATTERNS[@]}"; do
    case "${pattern}" in
      '') continue ;;
      */) SPECS+=(":(glob,icase)**/${pattern}**") ;;
      *) SPECS+=(":(glob,icase)**/${pattern}") ;;
    esac
  done
  [ "${#SPECS[@]}" -gt 0 ] || die "No reset patterns."
  # 'ls-files', not 'ls-tree', which rejects the ':(glob)' magic. The index
  # holds the head tree after the checkout; the base tree goes into a scratch
  # index. Listed into files so a git failure stops the run (set -e).
  git_safe ls-files -z -- "${SPECS[@]}" >"${WS}/pr-paths"
  GIT_INDEX_FILE="${WS}/base.index" git_safe read-tree "${BASE_SHA}"
  GIT_INDEX_FILE="${WS}/base.index" git_safe ls-files -z -- "${SPECS[@]}" >"${WS}/base-paths"
  mapfile -d '' -t PR_PATHS <"${WS}/pr-paths"
  mapfile -d '' -t BASE_PATHS <"${WS}/base-paths"
  rm -f "${WS}/pr-paths" "${WS}/base-paths" "${WS}/base.index"

  for path in "${PR_PATHS[@]}"; do
    # Git refuses such paths in trees, but they become filesystem paths here.
    case "/${path}/" in */../* | */./* | *//*) die "Unexpected path in PR tree: '${path}'." ;; esac
    # Only what the PR adds or changes is kept, so the review can comment on it.
    head_id=$(git_safe rev-parse "${HEAD_SHA}:${path}")
    base_id=$(git_safe rev-parse -q --verify "${BASE_SHA}:${path}" || true)
    [ "${head_id}" != "${base_id}" ] || continue
    dest="${CONTEXT_DIR}/pr-instruction-files/${path}.txt"
    mkdir -p "$(dirname "${dest}")"
    git_safe cat-file -p "${HEAD_SHA}:${path}" >"${dest}"
  done
  if [ "${#PR_PATHS[@]}" -gt 0 ]; then
    # Git removes paths without following symlinks in leading components.
    git_safe --literal-pathspecs rm -r -q -f --ignore-unmatch -- "${PR_PATHS[@]}"
  fi
  if [ "${#BASE_PATHS[@]}" -gt 0 ]; then
    git_safe --literal-pathspecs checkout -q "${BASE_SHA}" -- "${BASE_PATHS[@]}"
  fi
  verify_followed_files

  # --- Render the prompt -------------------------------------------------------
  PR_TITLE=$(jq -r '.title // "" | tostring' "${IN_DIR}/pr.json" | tr -d '\r' | head -n 1 | clip "${PR_TITLE_MAX_BYTES}")
  PR_BODY=$(jq -r '.body // "" | tostring' "${IN_DIR}/pr.json" | tr -d '\r' | clip "${PR_BODY_MAX_BYTES}")
  NONCE=$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')
  # The PR cannot know the nonce, but strip it anyway so the delimiters stay unique.
  PR_TITLE=${PR_TITLE//"${NONCE}"/}
  PR_BODY=${PR_BODY//"${NONCE}"/}

  if [ "${#RULE_FILES[@]}" -gt 0 ]; then
    RULES=$(i=0; for f in "${RULE_FILES[@]}"; do i=$((i + 1)); printf '%d. `%s`, if it exists.\n' "${i}" "${f}"; done)
  else
    RULES="No project rule files are configured."
  fi
  ADDENDUM=""
  if [ -s "${ADDENDUM_FILE}" ]; then
    ADDENDUM=$(printf '### Project notes\n\n%s\n' "$(clip 8192 <"${ADDENDUM_FILE}")")
  fi

  prompt=$(<"${TRUSTED_ROOT}/prompt.md.tmpl")
  # Trusted caller text first, so that placeholders inside it are expanded
  # like the template's own; untrusted values last.
  prompt=${prompt//"{{ADDENDUM}}"/"${ADDENDUM}"}
  prompt=${prompt//"{{RULES}}"/"${RULES}"}
  prompt=${prompt//"{{PROJECT}}"/"${PROJECT}"}
  prompt=${prompt//"{{REPO}}"/"${REPO}"}
  prompt=${prompt//"{{PR_NUMBER}}"/"${PR_NUMBER}"}
  prompt=${prompt//"{{HEAD_SHA}}"/"${HEAD_SHA}"}
  prompt=${prompt//"{{BASE_SHA}}"/"${BASE_SHA}"}
  prompt=${prompt//"{{MERGE_BASE}}"/"${MERGE_BASE}"}
  prompt=${prompt//"{{CONTEXT_DIR}}"/".agent-review-context"}
  prompt=${prompt//"{{NONCE}}"/"${NONCE}"}
  # Untrusted values last, so placeholders inside them are not expanded.
  prompt=${prompt//"{{PR_TITLE}}"/"${PR_TITLE}"}
  prompt=${prompt//"{{PR_BODY}}"/"${PR_BODY}"}
  printf '%s\n' "${prompt}" >"${PROMPT_FILE}"
  echo "run-review: fetched ${REPO}#${PR_NUMBER} at ${HEAD_SHA} (merge base ${MERGE_BASE})."
}

review_phase() {
  # The checkout and prompt come from the fetch phase, which ran no agent.
  [ -f "${PROMPT_FILE}" ] && [ -d "${REPO_DIR}/.git" ] || die "No prepared checkout in '${WS}'; run the fetch phase first."
  rm -rf "${HARNESS_OUT}" "${RESULT_DIR}" "${SCRATCH_HOME}" "${SCRATCH_TMP}"
  mkdir -p "${HARNESS_OUT}" "${RESULT_DIR}" "${SCRATCH_HOME}" "${SCRATCH_TMP}"
  export HOME="${SCRATCH_HOME}" TMPDIR="${SCRATCH_TMP}"

  # --- Run the harness -----------------------------------------------------------
  echo "run-review: running harness '${HARNESS}' on ${REPO}#${PR_NUMBER} at ${HEAD_SHA}."
  harness_exit=0
  (
    cd "${REPO_DIR}"
    export PROMPT_FILE REPO_DIR MODEL HARNESS_PREFIX PR_NUMBER CONTEXT_DIR STUB_PADDING_KIB
    # The harness writes to its own directory; only this script writes the result.
    OUT_DIR="${HARNESS_OUT}" exec timeout --kill-after=30s "${HARNESS_TIMEOUT_MINUTES}m" bash "${ADAPTER_DIR}/run.sh"
  ) || harness_exit=$?

  case "${harness_exit}" in
    0) status=ok ;;
    2) status=no-review ;;
    124 | 137) status=timeout ;;
    *) status=failed ;;
  esac

  # --- Collect the review ----------------------------------------------------------
  # The harness output is agent-controlled: accept only a regular file. The host
  # re-checks this ('collect-result'); this check keeps the failure readable.
  candidate="${HARNESS_OUT}/review.md"
  if [ "${status}" != ok ] || [ -L "${candidate}" ] || [ ! -f "${candidate}" ] || [ ! -s "${candidate}" ]; then
    [ "${status}" = ok ] && status=no-review
    write_meta "${status}" "${harness_exit}" false 0
    echo "run-review: no review produced (status '${status}', harness exit ${harness_exit})." >&2
    [ "${status}" = no-review ] && exit 2
    exit 1
  fi

  # Models often open their answer with a note ("Writing the final review
  # now.", "Maximum steps ... reached") although the prompt asks them to start
  # with '### Summary'. Keep the review from its first '### ' heading, if any.
  body="${RESULT_DIR}/review.body"
  if grep -q '^### ' <"${candidate}"; then
    awk 'f || /^### /{f = 1} f' <"${candidate}" >"${body}"
  else
    cat <"${candidate}" >"${body}"
  fi

  size=$(stat -c %s -- "${body}")
  truncated=false
  if [ "${size}" -gt "${REVIEW_MAX_BYTES}" ]; then
    truncated=true
    clip "$((REVIEW_MAX_BYTES - 100))" <"${body}" >"${RESULT_DIR}/review.md"
    printf '\n\n_[Review truncated: it exceeded %d bytes.]_\n' "${REVIEW_MAX_BYTES}" >>"${RESULT_DIR}/review.md"
  else
    { iconv -f UTF-8 -t UTF-8 -c 2>/dev/null || true; } <"${body}" >"${RESULT_DIR}/review.md"
  fi
  rm -f "${body}"
  write_meta ok "${harness_exit}" "${truncated}" "$(stat -c %s -- "${RESULT_DIR}/review.md")"
  echo "run-review: review written ($(stat -c %s -- "${RESULT_DIR}/review.md") bytes, truncated=${truncated})."
}

case "${PHASE}" in
  fetch) fetch_phase ;;
  review) review_phase ;;
  all)
    fetch_phase
    review_phase
    ;;
esac
