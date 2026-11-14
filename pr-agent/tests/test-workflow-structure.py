"""Structural checks of the PR Agent workflow.

Reads .github/workflows/pr-agent.yml with YAML aliases resolved (PyYAML) and
checks properties that must survive every edit:

- only 'pr-agent' declares an environment, and only it holds the key: one
  boolean check, and 'OPENAI.KEY' in the PR Agent step;
- no other job references a secret, except the boolean shared-key check in
  'authorize';
- 'pr-agent' runs no checkout and no script of ours, and the image is pinned
  by digest;
- its 'timeout-minutes' equals the JOB_TIMEOUT that 'Resolve models' uses;
- the PR Agent step tolerates failure and its timeout is a number;
- the settings that keep model output out of labels and the PR description;
- write permissions only in 'pr-agent' and 'report', never 'contents';
- no expression ('${{') inside a 'run:' script.

Offline. Run: python3 pr-agent/tests/test-workflow-structure.py
"""

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-agent.yml"

SHARED_KEY_CHECK = ("authorize", "Authorize", "HAS_SHARED_KEY", "${{ secrets.inference_api_key != '' }}")
KEY_REFS = {
    ("Check the inference key", "HAS_KEY"): "${{ secrets.inference_api_key != '' }}",
    ("Run PR Agent", "OPENAI.KEY"): "${{ secrets.inference_api_key }}",
}
SAFETY_ENV = {
    "pr_reviewer.enable_review_labels_effort": "false",
    "pr_reviewer.enable_review_labels_security": "false",
    "pr_description.publish_labels": "false",
    "pr_description.publish_description_as_comment": "true",
    "pr_reviewer.publish_error_details": "true",
    "github_action_config.auto_describe": "false",
    "github_action_config.auto_improve": "false",
    "OPENAI.API_BASE": "${{ needs.authorize.outputs.api_base }}",
}

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


def secret_refs(job: dict):
    """Yields (step name, env key, value) for every 'secrets.' reference in a job."""
    for key, value in job.items():
        if key != "steps" and "secrets." in str(value):
            yield None, key, str(value)
    for step in job.get("steps", []):
        for key, value in step.items():
            if key == "env":
                for env_key, env_value in value.items():
                    if "secrets." in str(env_value):
                        yield step.get("name"), env_key, str(env_value)
            elif "secrets." in str(value):
                yield step.get("name"), key, str(value)


def main() -> int:
    with WORKFLOW.open() as f:
        workflow = yaml.safe_load(f)
    jobs = workflow["jobs"]
    print("pr-agent.yml")

    check(workflow.get("permissions") == {}, "top-level 'permissions: {}'")
    check(
        [j for j, job in jobs.items() if "environment" in job] == ["pr-agent"]
        and jobs["pr-agent"]["environment"] == "${{ inputs.environment }}",
        "only 'pr-agent' declares an environment, the caller's",
    )

    for job_id, job in jobs.items():
        refs = list(secret_refs(job))
        if job_id == "pr-agent":
            check({(s, k): v for s, k, v in refs} == KEY_REFS and len(refs) == len(KEY_REFS),
                  f"'pr-agent' references the key only in {sorted(k for _, k in KEY_REFS)}")
        elif job_id == "authorize":
            check(refs == [SHARED_KEY_CHECK[1:]], "'authorize' references the key only in the shared-key check")
        else:
            check(not refs, f"job '{job_id}' references no secret {refs or ''}")

    agent = jobs["pr-agent"]
    steps = {s["name"]: s for s in agent["steps"]}
    check(not any("checkout" in s.get("uses", "") for job in jobs.values() for s in job.get("steps", [])),
          "no actions/checkout anywhere")
    check([s["name"] for s in agent["steps"] if "run" in s] == ["Check the inference key"],
          "'pr-agent' runs no script but the key check")
    run_step = steps["Run PR Agent"]
    check(re.fullmatch(r"docker://pragent/pr-agent@sha256:[0-9a-f]{64}", run_step["uses"]) is not None,
          "the image is pinned by digest")
    check(run_step.get("continue-on-error") is True, "the PR Agent step tolerates failure")
    check(run_step.get("timeout-minutes") == "${{ fromJSON(needs.authorize.outputs.step_timeout) }}",
          "the step timeout is a number derived by 'Resolve models'")
    env = run_step["env"]
    check(all(env.get(k) == v for k, v in SAFETY_ENV.items()),
          f"safety settings in the step env {[k for k, v in SAFETY_ENV.items() if env.get(k) != v] or ''}")
    check(agent["outputs"] == {"outcome": "${{ steps.pragent.outcome }}"} and run_step.get("id") == "pragent",
          "'pr-agent' exports the step outcome, not its conclusion")

    resolve = next(s for s in jobs["authorize"]["steps"] if s["name"] == "Resolve models")
    check(str(agent["timeout-minutes"]) == resolve["env"]["JOB_TIMEOUT"],
          f"'pr-agent' timeout-minutes ({agent['timeout-minutes']}) equals JOB_TIMEOUT ({resolve['env']['JOB_TIMEOUT']})")

    check("needs.authorize.outputs.allowed == 'true'" in agent["if"]
          and "needs.authorize.outputs.dry_run == 'false'" in agent["if"],
          "'pr-agent' runs only when allowed and not a dry run")
    report = jobs["report"]
    check("always()" in report["if"] and "needs.authorize.outputs.result == 'incomplete'" in report["if"],
          "'report' runs after a failed 'pr-agent' job, never after a dry run or a refusal")

    report_step = next(s for s in report["steps"] if s["name"] == "Report the PR Agent outcome on the PR")
    check(report_step.get("continue-on-error") is True, "the report step tolerates API errors")
    check(report.get("concurrency") == {"group": "pr-agent-report-${{ needs.authorize.outputs.pr }}",
                                         "cancel-in-progress": False},
          "'report' is serialized per pull request, never cancelled in progress")
    check(jobs["authorize"]["outputs"]["result"]
          == "${{ (steps.auth.outputs.allowed != 'true' || steps.models.outcome == 'success') && steps.auth.outputs.result || '' }}",
          "'authorize' reports no result if 'Resolve models' failed")
    # PyYAML reads the key "on" as True.
    result = workflow[True]["workflow_call"]["outputs"]["result"]["value"]
    check("jobs.pr-agent.outputs.outcome == 'success'" in result and "jobs.pr-agent.outputs.outcome == 'failure'" in result,
          "the workflow's result passes on only the step's 'success' and 'failure'")

    for job_id, job in jobs.items():
        permissions = job.get("permissions")
        check(isinstance(permissions, dict), f"job '{job_id}' sets its permissions explicitly")
        writes = sorted(k for k, v in (permissions or {}).items() if v == "write")
        expected = ["issues", "pull-requests"] if job_id in ("pr-agent", "report") else []
        check(writes == expected, f"job '{job_id}' write permissions: {writes}")

    offenders = [
        f"{job_id}/{step.get('name', i)}"
        for job_id, job in jobs.items()
        for i, step in enumerate(job.get("steps", []))
        if "${{" in step.get("run", "")
    ]
    check(not offenders, f"no '${{{{' inside a 'run:' script {offenders or ''}")

    print(f"{len(failures)} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
