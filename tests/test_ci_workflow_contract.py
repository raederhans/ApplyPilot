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
    assert set(jobs) == {
        "core",
        "compatibility",
        "browser-chromium",
        "windows-install-smoke",
    }

    core = jobs["core"]
    compatibility = jobs["compatibility"]
    browser = jobs["browser-chromium"]
    windows = jobs["windows-install-smoke"]
    assert all(
        isinstance(job, dict)
        for job in (core, compatibility, browser, windows)
    )
    versions = compatibility["strategy"]["matrix"]["python-version"]
    check_names = {core["name"]}
    check_names.update(
        compatibility["name"].replace("${{ matrix.python-version }}", version)
        for version in versions
    )
    check_names.add(browser["name"])
    check_names.add(windows["name"])

    assert check_names == {
        "Core Python 3.12",
        "Compatibility Python 3.11",
        "Compatibility Python 3.13",
        "Browser Chromium 3.12",
        "Windows 3.12 tests and clean install smoke",
    }

    core_steps = core["steps"]
    assert isinstance(core_steps, list)
    core_test = next(step for step in core_steps if step.get("name") == "Test core tier")
    assert core_test["run"] == 'python -m pytest -q -m "not browser and not windows"'

    compatibility_steps = compatibility["steps"]
    assert isinstance(compatibility_steps, list)
    compatibility_test = next(
        step for step in compatibility_steps
        if step.get("name") == "Test compatibility subset"
    )
    assert compatibility_test["run"] == (
        'python -m pytest -q -m "compatibility and not browser and not windows"'
    )
    browser_steps = browser["steps"]
    assert isinstance(browser_steps, list)
    chromium_step = next(step for step in browser_steps if step.get("name") == "Install Playwright Chromium")
    assert chromium_step["run"] == "python -m playwright install --with-deps chromium"

    windows_steps = windows["steps"]
    assert isinstance(windows_steps, list)
    windows_test = next(
        step for step in windows_steps if step.get("name") == "Test Windows tier"
    )
    assert windows_test["run"] == "python -m pytest -q -m windows"
    assert windows["needs"] == ["core", "compatibility", "browser-chromium"]

    bind_step = next(step for step in windows["steps"] if step.get("name") == "Bind isolated ApplyPilot workspace")
    assert bind_step["shell"] == "pwsh"
    assert "$env:RUNNER_TEMP" in bind_step["run"]
    assert "$env:GITHUB_ENV" in bind_step["run"]
