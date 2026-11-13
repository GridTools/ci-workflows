"""Structural checks of the agent review workflow (security review, M3/S7).

Reads the workflow files with YAML aliases resolved (PyYAML), the way GitHub
does, and checks properties that must survive every edit:

- only the keyed 'review' job declares an environment;
- a job without 'environment:' references no secret, except the one boolean
  check in 'authorize' that the key is not a shared secret;
- only 'publish' has a write permission;
- no expression ('${{') inside any 'run:' script, in any workflow file;
- 'review-keyless' runs the same steps as 'review' except the listed ones;
- the keyed job's event backstop, concurrency group and per-call artifact
  names.

Offline. Run: python3 agent-review/tests/test-workflow-structure.py
"""

import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
REVIEW_WORKFLOW = WORKFLOWS / "agent-review.yml"

# The only 'secrets.' reference allowed in a job without an environment.
SHARED_KEY_CHECK = ("authorize", "Authorize", "HAS_SHARED_KEY", "${{ secrets.inference_api_key != '' }}")
# Steps of 'review-keyless' that differ from those of 'review'.
KEYLESS_DIFFERENT = {"Check the inference key": "Check the harness wire"}

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


def strings(node, path=()):
    """Yields (path, string) for every string value in a YAML tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from strings(value, path + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from strings(value, path + (str(index),))
    elif isinstance(node, str):
        yield path, node


def load(path: pathlib.Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def check_runs_have_no_expressions(path: pathlib.Path) -> None:
    workflow = load(path)
    offenders = [
        f"{job_id}/{step.get('name', index)}"
        for job_id, job in workflow["jobs"].items()
        for index, step in enumerate(job.get("steps", []))
        if "${{" in step.get("run", "")
    ]
    check(not offenders, f"{path.name}: no '${{{{' inside a 'run:' script {offenders or ''}")


def main() -> int:
    workflow = load(REVIEW_WORKFLOW)
    jobs = workflow["jobs"]

    print("agent-review.yml")
    check(workflow.get("permissions") == {}, "top-level 'permissions: {}'")
    check(
        [job_id for job_id, job in jobs.items() if "environment" in job] == ["review"],
        "only 'review' declares an environment",
    )

    for job_id, job in jobs.items():
        if "environment" in job:
            continue
        refs = []
        for path, value in strings(job):
            if "secrets." not in value:
                continue
            step_name = job["steps"][int(path[1])].get("name") if path[0] == "steps" else None
            if (job_id, step_name, path[-1], value) == SHARED_KEY_CHECK:
                continue
            refs.append("/".join(path))
        check(not refs, f"job '{job_id}' (no environment) references no secret {refs or ''}")
    authorize_env = jobs["authorize"]["steps"][1].get("env", {})
    check(
        authorize_env.get("HAS_SHARED_KEY") == SHARED_KEY_CHECK[3],
        "'authorize' checks that the key is not a shared secret",
    )

    for job_id, job in jobs.items():
        permissions = job.get("permissions")
        check(isinstance(permissions, dict), f"job '{job_id}' sets its permissions explicitly")
        writes = sorted(k for k, v in (permissions or {}).items() if v == "write")
        expected = ["pull-requests"] if job_id == "publish" else []
        check(writes == expected, f"job '{job_id}' write permissions: {writes}")

    keyed, keyless = jobs["review"], jobs["review-keyless"]
    check(
        "github.event_name == 'pull_request_target'" in keyed["if"]
        and "github.event_name == 'workflow_dispatch'" in keyed["if"]
        and "needs.authorize.outputs.keyed == 'true'" in keyed["if"],
        "keyed job: event backstop and 'keyed' condition",
    )
    check("needs.authorize.outputs.keyed == 'false'" in keyless["if"], "keyless job: 'keyed' condition")
    group = keyed.get("concurrency", {}).get("group", "")
    check(
        group.startswith("agent-review-keyed-") and "needs.authorize.outputs.pr" in group,
        "keyed job: own concurrency group per pull request",
    )
    check(keyed["concurrency"].get("cancel-in-progress") is False, "keyed job: no cancelling")
    for key in ("permissions", "outputs", "env", "needs", "runs-on", "timeout-minutes"):
        check(keyed.get(key) == keyless.get(key), f"keyless job: same '{key}' as the keyed job")

    keyed_steps = {s["name"]: s for s in keyed["steps"]}
    keyless_names = [s["name"] for s in keyless["steps"]]
    check(
        keyless_names == [KEYLESS_DIFFERENT.get(s["name"], s["name"]) for s in keyed["steps"]],
        "keyless job: the same steps in the same order",
    )
    for step in keyless["steps"]:
        name = step["name"]
        if name in KEYLESS_DIFFERENT.values():
            check(step.get("id") == "key", f"keyless step '{name}': id 'key'")
            continue
        twin = keyed_steps.get(name)
        if name == "Run the review in the sandbox":
            check(
                {k: v for k, v in step.items() if k != "env"} == {k: v for k, v in twin.items() if k != "env"},
                f"keyless step '{name}': identical but for its env",
            )
            check(
                {k: v for k, v in twin["env"].items() if k != "KEY"} == step["env"] and "KEY" not in step["env"],
                f"keyless step '{name}': env without 'KEY' only",
            )
        else:
            check(step == twin, f"keyless step '{name}': identical")

    names = [
        value
        for job in jobs.values()
        for step in job.get("steps", [])
        if "artifact" in step.get("uses", "")
        for key, value in step.get("with", {}).items()
        if key == "name"
    ]
    check(
        bool(names) and all(n.endswith("-${{ needs.authorize.outputs.call_id }}") for n in names),
        f"artifact names carry the call id ({len(names)})",
    )
    check(
        all(
            "agent-review/api-bases.txt" not in v and "inputs.api_base" not in v
            for job_id, job in jobs.items()
            if job_id != "authorize"
            for _, v in strings(job)
        ),
        "only 'authorize' reads the 'api_base' input; later jobs use its allowlisted output",
    )

    for path in sorted(WORKFLOWS.glob("*.yml")):
        check_runs_have_no_expressions(path)

    print(f"{len(failures)} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
