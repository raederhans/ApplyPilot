from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

import install
from scripts.build_release import audit_wheel


def test_bundle_source_requires_matching_checksum(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = packages / "applypilot_local-0.4.0-py3-none-any.whl"
    wheel.write_bytes(b"verified wheel")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (tmp_path / "SHA256SUMS").write_text(f"{digest}  packages/{wheel.name}\n", encoding="utf-8")

    assert install.resolve_source(tmp_path) == str(wheel.resolve())

    wheel.write_bytes(b"tampered wheel")
    with pytest.raises(RuntimeError, match="Checksum verification failed"):
        install.resolve_source(tmp_path)


def test_jobboard_extra_supports_package_paths_and_vcs() -> None:
    assert install.with_extra("applypilot-local", True) == "applypilot-local[jobboards]"
    assert install.with_extra("C:/release/applypilot.whl", True).endswith(".whl[jobboards]")
    assert install.with_extra("git+https://example.test/repo.git", True) == (
        "applypilot-local[jobboards] @ git+https://example.test/repo.git"
    )


def _write_wheel(path: Path, *, include_private_file: bool = False) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("applypilot/__init__.py", "")
        archive.writestr("applypilot/frontend/dashboard.html", "<!doctype html>")
        archive.writestr(
            "applypilot_local-0.4.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: applypilot-local\nVersion: 0.4.0\n",
        )
        if include_private_file:
            archive.writestr("applypilot/profile.json", "{}")


def test_wheel_audit_requires_frontend_and_rejects_private_runtime_files(tmp_path: Path) -> None:
    public_wheel = tmp_path / "public.whl"
    _write_wheel(public_wheel)
    audit_wheel(public_wheel, "applypilot-local", "0.4.0")

    private_wheel = tmp_path / "private.whl"
    _write_wheel(private_wheel, include_private_file=True)
    with pytest.raises(RuntimeError, match="Private runtime file"):
        audit_wheel(private_wheel, "applypilot-local", "0.4.0")
