#!/usr/bin/env bash
# Installs the 'stub' harness, which makes no model calls.
#
# Usage: install.sh <prefix>
#
# Runs in the secretless 'prepare' job. Every adapter's install.sh must leave
# the harness in <prefix>, runnable without network access, and write its
# exact version to <prefix>/VERSION.

set -euo pipefail

[ $# -eq 1 ] || { echo "Usage: install.sh <prefix>" >&2; exit 1; }
mkdir -p "$1"
echo "stub-1" >"$1/VERSION"

# The Python runtime on the host, as the trusted entry points get it
# (bin/probe, one line; docs/adr/0003-python-runtime.md). run.sh prints the
# same line from inside the sandbox; the self-test compares the two.
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
/usr/bin/python3 -I -S -B "${root}/bin/probe"
