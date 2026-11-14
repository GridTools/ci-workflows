"""Thin wrapper around ``gh api``, run as a subprocess.

The only module that runs ``gh``, so the structure test can check that only
``authorize`` and ``publish-comment`` reach it (and with it the job token,
which ``gh`` reads from ``GH_TOKEN``; it never passes through Python).
``gh`` rather than ``urllib``: the token stays out of this process, proxy
variables are not honoured by accident, and the tests' fake ``gh`` on
``PATH`` keeps working.

Keep the projections in ``gh --jq``: a PR object or a comment body is
untrusted, and the host must not parse it (invariant 2 in
docs/agent-review.md). Python receives only the projected fields.

The argument order is fixed: ``gh api [--paginate] [-X METHOD] <endpoint>
[--input -] [--jq FILTER]``, as the bash scripts call it; the fakes in
tests/test-authorize.sh and tests/test-publish-comment.sh match on it.
"""

from agent_review import runner, validate

METHODS = ("GET", "POST", "PATCH", "PUT", "DELETE")
# A relative API path with an optional query: never an option ('-...'), a
# URL or a host.
ENDPOINT = validate.pattern(r"[A-Za-z0-9][A-Za-z0-9._~/%-]*(?:\?[A-Za-z0-9._~%=&-]*)?")


def api(
    endpoint: str,
    *,
    method: str = "GET",
    jq: str | None = None,
    paginate: bool = False,
    body: bytes | None = None,
    timeout: float | None = 120,
) -> bytes:
    """Runs ``gh api`` on the trusted host and returns its stdout.

    ``jq`` must be a constant of the calling code, never data. ``body`` is
    sent on stdin (``--input -``). A failure raises runner.CommandError,
    whose stderr holds gh's message.
    """
    validate.require(ENDPOINT, endpoint, "API endpoint")
    if method not in METHODS:
        raise ValueError(f"unsupported method {method!r}")
    if "//" in endpoint or "/../" in f"/{endpoint}/" or "/./" in f"/{endpoint}/":
        raise ValueError(f"invalid API endpoint {endpoint!r}")
    argv = ["gh", "api"]
    if paginate:
        argv.append("--paginate")
    if method != "GET":
        argv += ["-X", method]
    argv.append(endpoint)
    if body is not None:
        argv += ["--input", "-"]
    if jq is not None:
        argv += ["--jq", jq]
    return runner.run(runner.Zone.TRUSTED_HOST, argv, stdin=body, timeout=timeout).stdout
