#!/usr/bin/env python3
"""Exercise a built wheel in a disposable, clean ApplyPilot workspace."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def display_command(command: list[str]) -> str:
    """Return a log-safe command even when the parent console is non-UTF-8."""
    return " ".join(part if part.isascii() else ascii(part) for part in command)


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    print(f"+ {display_command(command)}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _find_wheel(explicit: Path | None) -> Path:
    if explicit:
        wheel = explicit.resolve()
        if not wheel.is_file():
            raise FileNotFoundError(f"Wheel not found: {wheel}")
        return wheel
    wheels = sorted((PROJECT_ROOT / "dist" / "python").glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected one built wheel, found {len(wheels)}")
    return wheels[0].resolve()


def smoke_release(wheel: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="applypilot release smoke ") as raw_root:
        root = Path(raw_root)
        environment = root / "isolated environment"
        workspace = root / "工作区"
        inputs = root / "starter files"
        inputs.mkdir()
        workspace.mkdir()

        resume = inputs / "resume.txt"
        profile = inputs / "profile.json"
        searches = inputs / "searches.yaml"
        dashboard = workspace / "dashboard.html"
        resume.write_text("Release Smoke Candidate\nPython, SQL, evidence review\n", encoding="utf-8")
        profile.write_text(
            json.dumps(
                {
                    "personal": {"full_name": "Release Smoke Candidate"},
                    "experience": {"target_role": "Software Engineer"},
                    "skills_boundary": {"programming_languages": ["Python"]},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        searches.write_text(
            'defaults:\n  location: "Remote"\nqueries:\n  - query: "Software Engineer"\n',
            encoding="utf-8",
        )

        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = _venv_python(environment)
        child_env = os.environ.copy()
        child_env["APPLYPILOT_DIR"] = str(workspace)
        child_env["PYTHONUTF8"] = "1"
        for secret_name in (
            "GEMINI_API_KEY",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "LLM_URL",
            "CAPSOLVER_API_KEY",
        ):
            child_env.pop(secret_name, None)

        _run([str(python), "-m", "pip", "install", str(wheel)], cwd=root, env=child_env)
        applypilot = environment / ("Scripts/applypilot.exe" if os.name == "nt" else "bin/applypilot")
        _run([str(applypilot), "resume-route", "--help"], cwd=root, env=child_env)
        base = [str(python), "-m", "applypilot.cli"]
        _run([*base, "--version"], cwd=root, env=child_env)
        _run(
            [
                *base,
                "init",
                "--resume",
                str(resume),
                "--profile",
                str(profile),
                "--searches",
                str(searches),
            ],
            cwd=root,
            env=child_env,
        )
        _run([*base, "doctor"], cwd=root, env=child_env)
        _run(
            [*base, "dashboard", "--no-open", "--output", str(dashboard)],
            cwd=root,
            env=child_env,
        )

        required = (
            workspace / "profile.json",
            workspace / "resume.txt",
            workspace / "searches.yaml",
            dashboard,
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise RuntimeError(f"Clean-workspace smoke missed outputs: {', '.join(missing)}")
        database = workspace / "applypilot.db"
        if database.exists():
            raise RuntimeError(f"Read-only dashboard unexpectedly created a database: {database}")
        html = dashboard.read_text(encoding="utf-8")
        if "__APPLYPILOT_DASHBOARD_DATA__" in html or "Opportunity Workbench" not in html:
            raise RuntimeError("Generated dashboard did not contain the packaged frontend")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args(argv)
    try:
        smoke_release(_find_wheel(args.wheel))
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Release smoke failed: {exc}", file=sys.stderr)
        return 1
    print("Release smoke passed: install -> resume-route help -> init -> doctor -> dashboard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
