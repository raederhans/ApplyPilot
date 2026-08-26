#!/usr/bin/env python3
"""Build and audit ApplyPilot Local release artifacts.

Outputs:

* ``dist/python/`` with one wheel and one source distribution;
* ``dist/applypilot-local-<version>-bundle.zip`` for verified local install;
* ``dist/SHA256SUMS`` covering every release artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {
    ".git",
    "apply-workers",
    "chrome-workers",
    "credentials",
    "docs/active",
    "receipts",
    "tools",
}
FORBIDDEN_NAMES = {".env", "applypilot.db", "profile.json", "resume.pdf", "resume.txt"}
FORBIDDEN_SUFFIXES = {".db", ".key", ".p12", ".pem", ".pfx", ".sqlite", ".sqlite3"}
SDIST_ALLOWED_ROOTS = {
    ".env.example",
    ".gitignore",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE.md",
    "PKG-INFO",
    "README.md",
    "SECURITY.md",
    "docs",
    "install.py",
    "profile.example.json",
    "pyproject.toml",
    "scripts",
    "src",
    "tests",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_identity(root: Path = PROJECT_ROOT) -> tuple[str, str]:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    return str(project["name"]), str(project["version"])


def _normalized_member(member: str, *, strip_sdist_root: bool) -> str:
    path = PurePosixPath(member.replace("\\", "/"))
    parts = list(path.parts)
    if strip_sdist_root and parts:
        parts = parts[1:]
    return "/".join(parts).strip("/")


def _assert_public_member(member: str) -> None:
    normalized = member.casefold().strip("/")
    if not normalized:
        return
    path = PurePosixPath(normalized)
    if path.name in FORBIDDEN_NAMES or path.suffix in FORBIDDEN_SUFFIXES:
        raise RuntimeError(f"Private runtime file entered a release archive: {member}")
    for forbidden in FORBIDDEN_PARTS:
        if normalized == forbidden or normalized.startswith(f"{forbidden}/") or f"/{forbidden}/" in f"/{normalized}/":
            raise RuntimeError(f"Forbidden workspace path entered a release archive: {member}")


def audit_wheel(path: Path, expected_name: str, expected_version: str) -> None:
    normalized_name = expected_name.replace("-", "_")
    metadata_suffix = ".dist-info/METADATA"
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        for member in members:
            normalized = _normalized_member(member, strip_sdist_root=False)
            _assert_public_member(normalized)
            allowed_prefixes = (
                "applypilot/",
                f"{normalized_name}-{expected_version}.dist-info/",
            )
            if normalized and not normalized.startswith(allowed_prefixes):
                raise RuntimeError(f"Unexpected wheel member: {member}")
        if "applypilot/frontend/dashboard.html" not in members:
            raise RuntimeError("Wheel is missing the packaged dashboard frontend")
        metadata_member = next((name for name in members if name.endswith(metadata_suffix)), None)
        if metadata_member is None:
            raise RuntimeError("Wheel is missing METADATA")
        metadata = BytesParser().parsebytes(archive.read(metadata_member))
        if metadata.get("Name") != expected_name or metadata.get("Version") != expected_version:
            raise RuntimeError(
                f"Wheel metadata mismatch: {metadata.get('Name')} {metadata.get('Version')}"
            )


def audit_sdist(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        for info in archive.getmembers():
            normalized = _normalized_member(info.name, strip_sdist_root=True)
            _assert_public_member(normalized)
            if normalized:
                top_level = normalized.split("/", 1)[0]
                if top_level not in SDIST_ALLOWED_ROOTS:
                    raise RuntimeError(f"Unexpected source-distribution root: {info.name}")


def audit_distributions(directory: Path, expected_name: str, expected_version: str) -> tuple[Path, Path]:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(
            f"Expected one wheel and one source distribution, found {len(wheels)} wheel(s) and {len(sdists)} sdist(s)"
        )
    audit_wheel(wheels[0], expected_name, expected_version)
    audit_sdist(sdists[0])
    return wheels[0], sdists[0]


def _write_checksums(path: Path, files: list[tuple[str, Path]]) -> None:
    lines = [f"{sha256(source)}  {display_name}" for display_name, source in files]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_bundle(
    destination: Path,
    *,
    project_name: str,
    version: str,
    wheel: Path,
    sdist: Path,
    root: Path = PROJECT_ROOT,
) -> None:
    bundle_root = f"{project_name}-{version}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=destination.parent) as raw_staging:
        staging = Path(raw_staging) / bundle_root
        packages = staging / "packages"
        packages.mkdir(parents=True)
        staged_wheel = shutil.copy2(wheel, packages / wheel.name)
        staged_sdist = shutil.copy2(sdist, packages / sdist.name)
        shutil.copy2(root / "install.py", staging / "install.py")
        shutil.copy2(root / "LICENSE", staging / "LICENSE")
        (staging / "README.txt").write_text(
            "ApplyPilot Local verified release bundle\n"
            "========================================\n\n"
            "Python 3.11 or 3.12 is recommended. From this directory run:\n\n"
            "    python install.py\n\n"
            "For the optional broad job-board connector:\n\n"
            "    python install.py --with-jobboards\n\n"
            "The installer verifies the bundled wheel, installs it through pipx,\n"
            "and never imports local profiles, resumes, credentials, or databases.\n",
            encoding="utf-8",
        )
        _write_checksums(
            staging / "SHA256SUMS",
            [
                (f"packages/{wheel.name}", staged_wheel),
                (f"packages/{sdist.name}", staged_sdist),
            ],
        )
        if destination.exists():
            destination.unlink()
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for source in sorted(staging.rglob("*")):
                if source.is_file():
                    archive.write(source, f"{bundle_root}/{source.relative_to(staging).as_posix()}")


def build_release(*, output_dir: Path, no_isolation: bool, root: Path = PROJECT_ROOT) -> list[Path]:
    name, version = project_identity(root)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    python_dir = output_dir / "python"
    if python_dir.exists():
        shutil.rmtree(python_dir)
    python_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".build-", dir=output_dir) as raw_build:
        build_dir = Path(raw_build)
        command = [sys.executable, "-m", "build", "--outdir", str(build_dir)]
        if no_isolation:
            command.append("--no-isolation")
        subprocess.run(command, cwd=root, check=True)
        wheel, sdist = audit_distributions(build_dir, name, version)
        wheel = shutil.copy2(wheel, python_dir / wheel.name)
        sdist = shutil.copy2(sdist, python_dir / sdist.name)

    bundle = output_dir / f"{name}-{version}-bundle.zip"
    create_bundle(bundle, project_name=name, version=version, wheel=wheel, sdist=sdist, root=root)
    checksum_file = output_dir / "SHA256SUMS"
    _write_checksums(
        checksum_file,
        [
            (f"python/{wheel.name}", wheel),
            (f"python/{sdist.name}", sdist),
            (bundle.name, bundle),
        ],
    )
    return [wheel, sdist, bundle, checksum_file]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=PROJECT_ROOT / "dist")
    parser.add_argument(
        "--no-isolation",
        action="store_true",
        help="Use the current environment for the PEP 517 build (useful when antivirus scans isolated temp environments).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        artifacts = build_release(output_dir=args.out_dir, no_isolation=args.no_isolation)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Release build failed: {exc}", file=sys.stderr)
        return 1
    print("Release artifacts:")
    for artifact in artifacts:
        print(f"  {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
