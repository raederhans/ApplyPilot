"""Immutable content/render storage and separate per-run process/verdict records.

The library's artifact is a content identity. Its current paths are a convenience
projection; render versions and historical application paths never get replaced.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

LAYOUT_VERSION = "source-arial-readable-v1"


def text_digest(text: str) -> str:
    canonical = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _immutable_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(value)
    except FileExistsError:
        if path.read_bytes() != value:
            raise ValueError(f"Immutable resume evidence differs: {path}")


def write_record(path: Path, value: Mapping[str, object]) -> None:
    _immutable_bytes(path, json.dumps(dict(value), ensure_ascii=False, indent=2, default=str).encode("utf-8"))


def library_root(path: Path) -> Path:
    for parent in path.resolve().parents:
        if parent.name == "resume-library":
            return parent
        if parent.name in {"tailored_resumes", "resume-runs"}:
            return parent.parent / "resume-library"
    return path.parent / "resume-library"


def ensure_version_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resume_render_versions (
            render_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL,
            text_path TEXT NOT NULL, pdf_path TEXT NOT NULL,
            pdf_sha256 TEXT NOT NULL, pdf_size INTEGER NOT NULL,
            layout_version TEXT NOT NULL, validation_report_path TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(artifact_id) REFERENCES resume_artifacts(artifact_id)
        )
    """)


def freeze_render(text_path: Path, text: str, artifact_id: str, report_path: str | None) -> dict:
    """Snapshot this exact PDF and source text; same text can have many renders."""
    pdf = text_path.with_suffix(".pdf").read_bytes()
    digest = hashlib.sha256(pdf).hexdigest()
    render_id = f"render:{text_digest(text)[:24]}:{digest}"
    root = library_root(text_path)
    render_root = root / "renders" / artifact_id.replace(":", "-") / digest
    frozen_text = render_root / "resume.txt"
    frozen_pdf = render_root / "resume.pdf"
    # Identical normalized text is stored canonically to permit CRLF-only aliases.
    canonical = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    _immutable_bytes(frozen_text, canonical.encode("utf-8"))
    _immutable_bytes(frozen_pdf, pdf)
    frozen_report = None
    layout_version = "legacy-unknown"
    if report_path and Path(report_path).is_file():
        report_bytes = Path(report_path).read_bytes()
        try:
            layout_version = json.loads(report_bytes).get("layout_version") or layout_version
        except (ValueError, AttributeError):
            pass
        report_digest = hashlib.sha256(report_bytes).hexdigest()
        target = root / "validation-evidence" / f"{report_digest}.json"
        _immutable_bytes(target, report_bytes)
        frozen_report = str(target)
    return {
        "render_id": render_id, "artifact_id": artifact_id,
        "text_path": str(frozen_text), "pdf_path": str(frozen_pdf),
        "pdf_sha256": digest, "pdf_size": len(pdf),
        "layout_version": layout_version,
        "validation_report_path": frozen_report,
        "created_at": datetime.now(UTC).isoformat(),
    }


def register_render(conn, render: Mapping[str, object]) -> None:
    ensure_version_schema(conn)
    fields = list(render)
    conn.execute(
        f"INSERT OR IGNORE INTO resume_render_versions ({','.join(fields)}) "
        f"VALUES ({','.join('?' for _ in fields)})", tuple(render[k] for k in fields),
    )


def start_resume_run(root: Path, job: Mapping[str, object], *, kind: str) -> Path:
    run = root / "resume-runs" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex)
    run.mkdir(parents=True, exist_ok=False)
    write_record(run / "input.json", {
        "run_id": run.name, "kind": kind,
        "recorded_at": datetime.now(UTC).isoformat(),
        "job": {k: job.get(k) for k in ("url", "title", "company_name", "full_description", "location")},
    })
    return run


def finish_resume_run(run: Path, report: Mapping[str, object], *, source_text: str = "",
                      supplemental_evidence: str = "") -> Path:
    """Keep model/process details separate from the validation verdict."""
    generation_keys = {"attempts", "generation_diagnostics", "local_repair", "route_context", "resume_routing"}
    write_record(run / "generation.json", {
        "run_id": run.name,
        **{k: v for k, v in report.items() if k in generation_keys},
        "source_resume_path": report.get("source_resume_path"),
        "source_text_digest": text_digest(source_text),
        "supplemental_text_digest": text_digest(supplemental_evidence),
        "layout_version": LAYOUT_VERSION,
    })
    _immutable_bytes(run / "source.txt", source_text.encode("utf-8"))
    if supplemental_evidence:
        _immutable_bytes(run / "supplemental.txt", supplemental_evidence.encode("utf-8"))
    target = run / "validation.json"
    write_record(target, {
        **{k: v for k, v in report.items() if k not in generation_keys},
        "run_id": run.name, "generation_record": str(run / "generation.json"),
        "layout_version": LAYOUT_VERSION,
        "renderer_sha256": hashlib.sha256((Path(__file__).parent / "scoring" / "pdf.py").read_bytes()).hexdigest(),
    })
    return target


def profile_fact_snapshot(profile: Mapping[str, object]) -> dict:
    """Only resume-relevant facts; not passwords, salary, or submission policy."""
    personal = profile.get("personal", {})
    return {
        "personal": {k: personal.get(k) for k in ("full_name", "preferred_display_name", "email", "phone")}
        if isinstance(personal, Mapping) else {},
        **{k: profile.get(k) for k in ("education", "experience", "current_employment",
                                       "project_references", "resume_facts", "skills_boundary")},
    }


def health_input_digest(artifact: Mapping[str, object], profile: Mapping[str, object]) -> str:
    """Invalidate cached assessment when its factual/source/layout inputs change."""
    inputs = {"facts": profile_fact_snapshot(profile),
              "layout": profile.get("tailoring", {}).get("resume_layout", {})}
    for key in ("text_path", "source_resume_path"):
        path = Path(str(artifact.get(key) or ""))
        inputs[key] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    metadata = json.loads(str(artifact.get("metadata_json") or "{}"))
    supplemental = Path(str(metadata.get("supplemental_evidence_path") or ""))
    inputs["supplemental"] = hashlib.sha256(supplemental.read_bytes()).hexdigest() if supplemental.is_file() else None
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()


def changed_used_facts(before: object, after: object, text: str, prefix: str = "") -> list[str]:
    """Flag changed old values actually used in a resume, without mass rewriting.

    New facts that were not used do not invalidate every existing resume. This
    is an impact hint, not a claim that a paraphrased fact was fully verified.
    """
    if before is None and isinstance(after, Mapping):
        before = {}
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        return [path for key in dict.fromkeys([*before, *after]) for path in changed_used_facts(
            before.get(key), after.get(key), text, f"{prefix}.{key}".strip("."))]
    if isinstance(before, list) and isinstance(after, list):
        return [path for i, value in enumerate(before) for path in changed_used_facts(
            value, after[i] if i < len(after) else None, text, f"{prefix}[{i}]"
        )]
    old = " ".join(str(before or "").casefold().split())
    if not before and after and prefix in {"personal.email", "personal.phone"}:
        header = "\n".join(text.splitlines()[:3])
        if prefix.endswith("email"):
            observed = re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", header)
            conflict = observed and str(after).casefold() not in {value.casefold() for value in observed}
        else:
            observed = [re.sub(r"\D", "", value) for value in re.findall(r"\+?\d[\d ()-]{6,}\d", header)]
            expected = re.sub(r"\D", "", str(after))
            conflict = observed and not any(expected.endswith(value) or value.endswith(expected) for value in observed)
        if conflict:
            return [prefix]
    if before != after and len(old) >= 3 and old in " ".join(text.casefold().split()):
        return [prefix]
    return []
