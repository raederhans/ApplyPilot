from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).parents[1]
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOW_DIRECTORY / "ci.yml"


def _load_workflow(path: Path) -> dict[str, object]:
    workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict), f"{path.name} must contain a workflow mapping"
    return workflow


def test_job_level_env_does_not_use_runner_context() -> None:
    violations: list[str] = []
    for path in sorted(WORKFLOW_DIRECTORY.glob("*.y*ml")):
        jobs = _load_workflow(path).get("jobs", {})
        assert isinstance(jobs, dict) and jobs, f"{path.name} must define at least one job"
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            env = job.get("env", {})
            if isinstance(env, dict) and any("${{ runner." in value for value in env.values()):
                violations.append(f"{path.name}:{job_id}")

    assert not violations, (
        "runner context is unavailable in job-level env; bind RUNNER_TEMP from a step instead: "
        + ", ".join(violations)
    )


def test_ci_exposes_all_required_checks() -> None:
    jobs = _load_workflow(CI_WORKFLOW)["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"verify", "windows-install-smoke"}

    verify = jobs["verify"]
    windows = jobs["windows-install-smoke"]
    assert isinstance(verify, dict) and isinstance(windows, dict)
    versions = verify["strategy"]["matrix"]["python-version"]
    check_names = {verify["name"].replace("${{ matrix.python-version }}", version) for version in versions}
    check_names.add(windows["name"])

    assert check_names == {
        "Python 3.11",
        "Python 3.12",
        "Python 3.13",
        "Windows 3.12 core tests and clean install smoke",
    }

    bind_step = next(step for step in windows["steps"] if step.get("name") == "Bind isolated ApplyPilot workspace")
    assert bind_step["shell"] == "pwsh"
    assert "$env:RUNNER_TEMP" in bind_step["run"]
    assert "$env:GITHUB_ENV" in bind_step["run"]
