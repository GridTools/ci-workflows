"""Structural checks of the agent review workflow (security review, M3/S7).

The checks and what they enforce are described in workflow_structure.py.
Reads the workflow files with YAML aliases resolved (PyYAML), the way GitHub
does, and the tracked files under agent-review/ (git ls-files).

Offline. Run: python3 agent-review/tests/test-workflow-structure.py
"""

import sys

from workflow_structure import (
    KEYLESS_DIFFERENT,
    REVIEW_WORKFLOW,
    SHARED_KEY_CHECK,
    WORKFLOWS,
    ast_problems,
    call_site_problems,
    collect_gate_problems,
    entry_problems,
    load,
    python_word_problems,
    repository,
    sandbox_output_problems,
    shebang_problems,
    shell_python_problems,
    strings,
    token_reach_problems,
    tree_problems,
)

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


def check_problems(problems: list[str], message: str) -> None:
    check(not problems, message)
    for problem in problems:
        print(f"          {problem}")


def check_runs_have_no_expressions(path) -> None:
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

    repo = repository()
    kinds = repo["kinds"]
    print("agent-review/ (Python runtime rules, docs/adr/0003-python-runtime.md)")
    print("  entry points: " + ", ".join(f"{name} ({kind})" for name, kind in sorted(kinds.items())))
    check_problems(call_site_problems(workflow, kinds), "call sites: roots per job and step, call form per file")
    check_problems(python_word_problems(workflow, kinds), "agent-review.yml: no other Python, no pip, no setup-python")
    check_problems(shell_python_problems(repo["shell"], kinds), "shell files: Python only in the exact call form")
    check_problems(
        sandbox_output_problems(workflow, repo["shell"]["harnesses/common.sh"]),
        "sandbox output: prefixed, no carriage return, no legacy '##[' (awf steps and diag)",
    )
    check_problems(collect_gate_problems(workflow), "'Collect the result' runs only after a successful teardown")
    check_problems(entry_problems(repo["entries"], repo["lib"]), "bin/: only the exact Python entry-point template")
    check_problems(tree_problems(repo["index"]), "tree: no symlink, .pth, .pyc, __pycache__, *customize.py")
    check_problems(shebang_problems(repo["first_lines"]), "no Python shebang outside lib/agent_review/ and tests/")
    check_problems(ast_problems(repo["python"]), "Python: allowlisted imports, no dynamic code, no shell")
    check_problems(token_reach_problems(repo["python"]), "token reach: agent_review.gh only from authorize, publish")

    print(f"{len(failures)} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
