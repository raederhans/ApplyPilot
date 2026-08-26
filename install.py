#!/usr/bin/env python3
"""Install ApplyPilot Local as an isolated command-line application.

The installer works in three contexts, in this order:

1. an extracted release bundle containing ``packages/*.whl``;
2. a source checkout containing ``pyproject.toml``;
3. the published ``applypilot-local`` distribution on PyPI.

It deliberately does not copy profiles, credentials, databases, resumes, or
workspace policy. Those remain user-owned runtime data under ``~/.applypilot``.
"""

from __future__ import annotations

import argparse
import hashlib
import shlex
import subprocess
import sys
from pathlib import Path

DIST_NAME = "applypilot-local"
MIN_PYTHON = (3, 11)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_bundle_hash(root: Path, wheel: Path) -> str | None:
    checksum_file = root / "SHA256SUMS"
    if not checksum_file.exists():
        return None
    expected_name = f"packages/{wheel.name}"
    for raw_line in checksum_file.read_text(encoding="utf-8").splitlines():
        parts = raw_line.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[1].lstrip("*").replace("\\", "/") == expected_name:
            return parts[0].casefold()
    raise RuntimeError(f"Release checksum does not list {expected_name}")


def resolve_source(root: Path) -> str:
    """Return the safest install source available beside this installer."""
    packages = root / "packages"
    wheels = sorted(packages.glob("applypilot_local-*.whl")) if packages.is_dir() else []
    if len(wheels) > 1:
        raise RuntimeError("Release bundle contains more than one ApplyPilot Local wheel")
    if wheels:
        wheel = wheels[0]
        expected = _expected_bundle_hash(root, wheel)
        if expected and _sha256(wheel) != expected:
            raise RuntimeError(f"Checksum verification failed for {wheel.name}")
        return str(wheel.resolve())
    if (root / "pyproject.toml").is_file():
        return str(root.resolve())
    return DIST_NAME


def with_extra(source: str, include_jobboards: bool) -> str:
    """Add the optional job-board extra to a package, path, or VCS source."""
    if not include_jobboards:
        return source
    if source.startswith("git+"):
        return f"{DIST_NAME}[jobboards] @ {source}"
    return f"{source}[jobboards]"


def _display_command(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _run(command: list[str], *, dry_run: bool) -> None:
    print(f"+ {_display_command(command)}")
    if not dry_run:
        subprocess.run(command, check=True)


def install(
    source: str,
    *,
    include_jobboards: bool,
    ensure_path: bool,
    dry_run: bool,
) -> None:
    if sys.version_info < MIN_PYTHON:
        required = ".".join(map(str, MIN_PYTHON))
        raise RuntimeError(f"ApplyPilot Local requires Python {required} or newer")
    if include_jobboards and sys.version_info >= (3, 13):
        raise RuntimeError("The optional job-board connector currently requires Python 3.11 or 3.12")

    pipx = [sys.executable, "-m", "pipx"]
    probe = subprocess.run(
        [*pipx, "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ) if not dry_run else None
    if probe is not None and probe.returncode != 0:
        print("pipx is not installed; bootstrapping it into the current user account.")
        _run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", "pipx"], dry_run=False)

    target = with_extra(source, include_jobboards)
    _run([*pipx, "install", "--force", "--python", sys.executable, target], dry_run=dry_run)
    if ensure_path:
        _run([*pipx, "ensurepath"], dry_run=dry_run)

    if not dry_run:
        print("\nApplyPilot Local is installed.")
        print("Open a new terminal, then run: applypilot init")
        print("Verify the installation with: applypilot doctor")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        help=(
            "Override the install source (package name, wheel path, local checkout, "
            "or git+https URL). By default the installer discovers a bundled wheel "
            "or local checkout before falling back to PyPI."
        ),
    )
    parser.add_argument(
        "--with-jobboards",
        action="store_true",
        help="Install the optional broad job-board connector (Python 3.11-3.12 only).",
    )
    parser.add_argument("--no-ensurepath", action="store_true", help="Do not ask pipx to add its app directory to PATH.")
    parser.add_argument("--dry-run", action="store_true", help="Print the installation commands without changing the system.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parent
    try:
        source = args.source or resolve_source(root)
        install(
            source,
            include_jobboards=args.with_jobboards,
            ensure_path=not args.no_ensurepath,
            dry_run=args.dry_run,
        )
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
