from pathlib import Path

import pytest

from applypilot.apply.runtime_namespace import RuntimeNamespace


def _namespace(
    root: Path,
    *,
    run_id: str = "run-1",
    session_id: str = "session-1",
    profile_id: str = "edge:worker:0",
) -> RuntimeNamespace:
    return RuntimeNamespace(
        root=root,
        run_id=run_id,
        session_id=session_id,
        profile_id=profile_id,
    )


def test_run_session_profile_and_output_namespaces_cannot_alias(tmp_path: Path) -> None:
    base = _namespace(tmp_path)
    variants = [
        _namespace(tmp_path, run_id="run-2"),
        _namespace(tmp_path, session_id="session-2"),
        _namespace(tmp_path, profile_id="edge:worker:1"),
    ]

    outputs = {base.output_root, *(item.output_root for item in variants)}

    assert len(outputs) == 4
    assert all(path.is_relative_to(tmp_path) for path in outputs)
    assert base.path("agent-turn-report.json").parent == base.output_root
    assert base.as_dict()["output_root"] == str(base.output_root)


@pytest.mark.parametrize("name", ["", ".", "..", "../report.json", "a/report.json"])
def test_namespace_rejects_output_traversal(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="one direct child"):
        _namespace(tmp_path).path(name)
