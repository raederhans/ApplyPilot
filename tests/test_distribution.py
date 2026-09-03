from __future__ import annotations

import hashlib
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import install
from applypilot import cli
from scripts.build_release import audit_wheel
from scripts.smoke_release import display_command

pytestmark = pytest.mark.compatibility


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


def test_public_brand_metadata_preserves_compatibility_identifiers() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = CliRunner().invoke(cli.app, ["--version"])

    assert version.exit_code == 0
    assert "CapyPilot" in version.stdout
    assert metadata["description"].startswith("CapyPilot:")
    assert metadata["authors"] == [{"name": "Pickle-Pixel and CapyPilot contributors"}]
    assert metadata["name"] == "applypilot-local"
    assert metadata["scripts"] == {"applypilot": "applypilot.cli:app"}
    assert metadata["urls"]["Repository"] == "https://github.com/raederhans/ApplyPilot"
    assert cli.app.info.name == "applypilot"


def test_capypilot_dashboard_assets_are_included_in_build_artifacts() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    artifacts = project["tool"]["hatch"]["build"]["artifacts"]
    asset_root = Path("src/applypilot/frontend/assets/capypilot")

    assert "src/applypilot/frontend/assets/capypilot/*" in artifacts
    assert {path.name for path in asset_root.iterdir() if path.is_file()} >= {
        "capypilot-lockup-light.png",
        "capypilot-mark-compact-master.png",
        "capypilot-mascot-companion.png",
        "favicon.ico",
        "favicon-16.png",
        "favicon-32.png",
        "favicon-48.png",
        "app-icon-192.png",
        "app-icon-512.png",
    }


def test_installer_displays_capy_pilot_and_keeps_applypilot_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(install, "_run", lambda *args, **kwargs: None)

    install.install("applypilot-local", include_jobboards=False, ensure_path=False, dry_run=False)

    output = capsys.readouterr().out
    assert "CapyPilot is installed." in output
    assert "applypilot init" in output
    assert "applypilot doctor" in output
    assert install.DIST_NAME == "applypilot-local"


def test_release_smoke_command_preview_is_ascii_safe() -> None:
    preview = display_command(["python", "--output", "C:/release workspace/工作区/dashboard.html"])

    assert preview.isascii()
    assert "\\u5de5\\u4f5c\\u533a" in preview


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
