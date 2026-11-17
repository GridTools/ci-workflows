#!/usr/bin/env bash
# The adapters' request timeouts against an inference service that stalls
# (fake_inference.py): a request that stalls must end and be retried, a
# slow but live stream must not be cut, and a run must get through long
# waits: a request answered only after 185 s (a wait seen with the real
# service), the same wait between the headers and the first event of a
# stream, and a service that answers nothing for 17 minutes (13 were seen;
# more than Pi's default of 3 retries can sit out, so the case fails if its
# raised retry count is lost).
#
# Usage: test-harness-stall.sh [--fast] [opencode] [pi]
#
# NOT part of the self-test: it installs the pinned harnesses (about
# 400 MB) and RUNS them on this machine, outside any sandbox, against a
# local server and an empty repository; they get no key. Run it after
# changing an adapter's timeouts or its pinned version. Needs jq, npm,
# curl and python3.
#
# By default the adapters run unchanged and the cases take the real times
# (about 18 minutes; the cases run in parallel). --fast runs copies of the
# adapters with every timeout and retry delay divided by 30 (about 1
# minute): it checks the behaviour, not the values.

set -uo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

HARNESSES_DIR=$(cd "${BIN_DIR}/../harnesses" && pwd)
FAKE="$(dirname "${BIN_DIR}")/tests/fake_inference.py"
SCALE=1
SELECTED=()
for arg in "$@"; do
  case "${arg}" in
    --fast) SCALE=30 ;;
    opencode | pi) SELECTED+=("${arg}") ;;
    *)
      echo "Usage: test-harness-stall.sh [--fast] [opencode] [pi]" >&2
      exit 1
      ;;
  esac
done
[ "${#SELECTED[@]}" -gt 0 ] || SELECTED=(opencode pi)

WORK=$(mktemp -d)
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "${WORK}"' EXIT

# The constants an adapter declares, in milliseconds ('readonly NAME=value').
constant() { # harness name
  sed -n -E "s/^readonly (.* )?$2=([0-9]+)( .*)?\$/\\2/p" "${ADAPTERS}/$1/run.sh" | head -n 1
}

ADAPTERS=${HARNESSES_DIR}
if [ "${SCALE}" -ne 1 ]; then
  ADAPTERS="${WORK}/harnesses"
  cp -R "${HARNESSES_DIR}" "${ADAPTERS}"
  for name in HEADER_TIMEOUT_MS CHUNK_TIMEOUT_MS IDLE_TIMEOUT_MS RETRY_BASE_DELAY_MS; do
    for file in "${ADAPTERS}"/*/run.sh; do
      value=$(sed -n -E "s/^readonly (.* )?${name}=([0-9]+)( .*)?\$/\\2/p" "${file}" | head -n 1)
      [ -z "${value}" ] || sed -i -E "s/^(readonly (.* )?${name}=)${value}/\\1$((value / SCALE))/" "${file}"
    done
  done
fi

# Seconds: how long each harness waits for headers and between chunks; the
# 185 s of the slow answer; and a silence longer than 4
# attempts of Pi's timeout (3 retries, Pi's default) but shorter than what
# either harness must sit out.
OPENCODE_HEADER=$(($(constant opencode HEADER_TIMEOUT_MS) / 1000))
OPENCODE_CHUNK=$(($(constant opencode CHUNK_TIMEOUT_MS) / 1000))
PI_IDLE=$(($(constant pi IDLE_TIMEOUT_MS) / 1000))
SILENCE=$((1050 / SCALE))
SLOW_ANSWER=$((185 / SCALE))
echo "opencode: headers ${OPENCODE_HEADER} s, chunks ${OPENCODE_CHUNK} s; pi: idle ${PI_IDLE} s; silence ${SILENCE} s"

PORT=${STALL_TEST_PORT:-18200}
# Starts one case in the background: a server with the given options and
# the adapter against it. Results go to ${WORK}/<case>/.
start_case() { # case harness server-options...
  local name=$1 harness=$2 dir="${WORK}/$1" port
  shift 2
  PORT=$((PORT + 1))
  port=${PORT}
  mkdir -p "${dir}/repo" "${dir}/out" "${dir}/tmp"
  git -C "${dir}/repo" init -q
  echo "A file." >"${dir}/repo/README.md"
  echo 'Reply with a short review that starts with "### Summary". Use no tools.' >"${dir}/prompt.md"
  (
    python3 -I "${FAKE}" --port "${port}" --log "${dir}/server.jsonl" "$@" &
    server=$!
    sleep 1
    begin=${SECONDS}
    env -i PATH=/usr/bin:/bin HOME=/nonexistent TMPDIR="${dir}/tmp" PROMPT_FILE="${dir}/prompt.md" \
      REPO_DIR="${dir}/repo" OUT_DIR="${dir}/out" CONTEXT_DIR="${dir}/out" MODEL=fake/model PR_NUMBER=1 \
      HARNESS_PREFIX="${PREFIXES}/${harness}" AWF_API_PROXY_OPENAI_URL="http://127.0.0.1:${port}" \
      timeout 1500 bash "${ADAPTERS}/${harness}/run.sh" >"${dir}/adapter.log" 2>&1
    echo "$?" >"${dir}/exit"
    echo "$((SECONDS - begin))" >"${dir}/elapsed"
    kill "${server}" 2>/dev/null
  ) &
}

events() { jq -c 'select(.ev == "'"$2"'")' "${WORK}/$1/server.jsonl" | wc -l; } # case event
succeeded() { [ "$(cat "${WORK}/$1/exit")" = 0 ] && grep -q '^### Summary' "${WORK}/$1/out/review.md"; }
elapsed_between() { # case min max
  local t
  t=$(cat "${WORK}/$1/elapsed")
  [ "${t}" -ge "$2" ] && [ "${t}" -le "$3" ]
}
# The longest a stalled request was held before the adapter gave it up.
longest_wait() { jq -s '[.[] | select(.ev == "client-abort") | .waited] | max // 0 | floor' "${WORK}/$1/server.jsonl"; }

check_case() { # description command...
  TESTS_RUN=$((TESTS_RUN + 1))
  local msg=$1
  shift
  if "$@"; then echo "- ok: ${msg}"; else fail "${msg}"; fi
}

PREFIXES="${WORK}/prefix"
for harness in "${SELECTED[@]}"; do
  echo "Installing ${harness} ..."
  bash "${HARNESSES_DIR}/${harness}/install.sh" "${PREFIXES}/${harness}" || exit 1
done

for harness in "${SELECTED[@]}"; do
  # opencode makes a second request (the session title) next to the first
  # one, so two requests must stall for the review's own request to.
  start_case "${harness}-headers" "${harness}" --stall-headers 2
  start_case "${harness}-stream" "${harness}" --stall-midstream 2
  start_case "${harness}-slow" "${harness}" --stream-gap "$((330 / SCALE / 9 + 1))" --stream-chunks 9
  start_case "${harness}-late" "${harness}" --min-wait "${SLOW_ANSWER}"
  start_case "${harness}-first" "${harness}" --first-event-delay "${SLOW_ANSWER}"
  start_case "${harness}-silence" "${harness}" --ready-at "${SILENCE}"
done
echo "Running $((6 * ${#SELECTED[@]})) cases ..."
wait

for harness in "${SELECTED[@]}"; do
  if [ "${harness}" = opencode ]; then header=${OPENCODE_HEADER} chunk=${OPENCODE_CHUNK}; else header=${PI_IDLE} chunk=${PI_IDLE}; fi
  check_case "${harness}: a request without response headers ends after ${header} s and the retry succeeds" \
    eval "succeeded ${harness}-headers && [ \"\$(longest_wait ${harness}-headers)\" -ge $((header - 1)) ] && [ \"\$(longest_wait ${harness}-headers)\" -le $((header + 5)) ]"
  check_case "${harness}: a stream that stops ends after ${chunk} s and the retry succeeds" \
    eval "succeeded ${harness}-stream && [ \"\$(longest_wait ${harness}-stream)\" -ge $((chunk - 1)) ] && [ \"\$(longest_wait ${harness}-stream)\" -le $((chunk + 5)) ]"
  check_case "${harness}: a slow stream longer than the chunk timeout is not cut" \
    eval "succeeded ${harness}-slow && [ \"\$(events ${harness}-slow client-abort)\" -eq 0 ] && elapsed_between ${harness}-slow $((chunk + 1)) 100000"
  check_case "${harness}: a request answered only after ${SLOW_ANSWER} s succeeds at the first attempt" \
    eval "succeeded ${harness}-late && [ \"\$(events ${harness}-late client-abort)\" -eq 0 ]"
  check_case "${harness}: a stream whose first event comes ${SLOW_ANSWER} s after its headers is not cut" \
    eval "succeeded ${harness}-first && [ \"\$(events ${harness}-first client-abort)\" -eq 0 ]"
  check_case "${harness}: a run sits out ${SILENCE} s of silence and then succeeds" \
    eval "succeeded ${harness}-silence && elapsed_between ${harness}-silence ${SILENCE} $((SILENCE + header + 20))"
done
for dir in "${WORK}"/*-*/; do
  [ -f "${dir}/server.jsonl" ] || continue
  echo "$(basename "${dir}"): exit $(cat "${dir}/exit"), $(cat "${dir}/elapsed") s: $(jq -c '[.i, .ev, .t]' "${dir}/server.jsonl" | tr '\n' ' ')"
done
summary
