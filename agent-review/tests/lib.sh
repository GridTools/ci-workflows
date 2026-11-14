# Minimal assertion helpers for the agent-review shell tests.
# shellcheck shell=bash

TESTS_RUN=0
TESTS_FAILED=0
# shellcheck disable=SC2034 # used by the test scripts
BIN_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../bin" && pwd)

# The first line of every Python entry point (ENTRY_TEMPLATE in
# workflow_structure.py, which also checks the rest of the file).
PYTHON_ENTRY_MARKER="# agent-review Python entry point: run only as '/usr/bin/python3 -I -S -B <root>/bin/<name>'."

# Sets ENTRY_CMD to the production command line of a bin/ entry point, by
# the file's own kind, as workflow_structure.py decides it: a Python entry
# point runs as '/usr/bin/python3 -I -S -B <file>', a bash script with
# /usr/bin/bash. Anything else is an error. Builtins and absolute paths
# only: some tests put fake tools first on PATH.
entry_command() { # file
  local first=""
  IFS= read -r first <"$1" || [ -n "${first}" ] || {
    echo "entry_command: cannot read '$1'" >&2
    return 1
  }
  case "${first}" in
    '#!/usr/bin/env bash' | '#!/usr/bin/bash') ENTRY_CMD=(/usr/bin/bash "$1") ;;
    "${PYTHON_ENTRY_MARKER}") ENTRY_CMD=(/usr/bin/python3 -I -S -B "$1") ;;
    *)
      echo "entry_command: '$1' is neither a bash script nor a Python entry point" >&2
      return 1
      ;;
  esac
}

# Runs a bin/ entry point with its production command line (entry_command).
# For a wrapper such as 'timeout', which cannot run a function, call
# entry_command and run "${ENTRY_CMD[@]}" instead.
run_entry() { # file args...
  local -a ENTRY_CMD=()
  entry_command "$1" || return 1
  shift
  "${ENTRY_CMD[@]}" "$@"
}

fail() {
  echo "  FAIL: $*"
  TESTS_FAILED=$((TESTS_FAILED + 1))
}

# Runs one test function in a fresh temporary directory, in a subshell.
run_test() {
  local name=$1 tmp rc=0
  tmp=$(mktemp -d)
  TESTS_RUN=$((TESTS_RUN + 1))
  echo "- ${name}"
  (cd "${tmp}" && TMP="${tmp}" "${name}") || rc=$?
  [ "${rc}" -eq 0 ] || fail "${name} (exit ${rc})"
  rm -rf "${tmp}"
}

assert() { # message command...
  local msg=$1
  shift
  "$@" || {
    echo "    assertion failed: ${msg}"
    return 1
  }
}

summary() {
  echo "${TESTS_RUN} tests, ${TESTS_FAILED} failed."
  [ "${TESTS_FAILED}" -eq 0 ]
}
