"""Small, source-preserving editorial improvements for existing library text.

No model calls and no new claims: experience chronology, relevant project order,
scannable education, and a clean contact header. The caller owns rendering and
approval; proposed text is never silently written over a library artifact.
"""

import re
from collections import Counter

from applypilot.scoring.pdf import parse_entries, parse_resume
from applypilot.scoring.resume_plan import order_entries_by_recency


def apply_reviewed_edits(text: str, edits: list[dict]) -> dict:
    """Apply explicit, source-cited editorial decisions; never infer new claims."""
    changes = []
    for edit in edits:
        before, after = edit["before"], edit["after"]
        if not before or text.count(before) != 1:
            raise ValueError("Reviewed edit must match exactly one current passage")
        if not str(edit.get("source_evidence", "")).strip() or not str(edit.get("reason", "")).strip():
            raise ValueError("Reviewed edit requires source evidence and a reason")
        if before == after:
            continue
        text = text.replace(before, after, 1)
        changes.append({"operation": "reviewed_content_edit", **edit, "claims_changed": True})
    return {"text": text, "changes": changes}


def refine_existing_text(text: str, *, relevance_terms: list[str] | None = None,
                         supplemental_text: str = "", project_titles: list[str] | None = None,
                         omit_summary: bool = False, summary_replacements: dict[str, str] | None = None,
                         refresh_project_titles: list[str] | None = None) -> dict:
    parsed = parse_resume(text)
    original_first_section = next(iter(parsed["section_order"]), "\0")
    sections = dict(parsed["sections"])
    changes = []
    added_bullets = []
    removed_bullets = []
    for before, after in (summary_replacements or {}).items():
        if before not in sections.get("SUMMARY", ""):
            raise ValueError("Reviewed summary phrase not found")
        sections["SUMMARY"] = sections["SUMMARY"].replace(before, after)
        changes.append({"section": "SUMMARY", "operation": "reviewed_precision_correction",
                        "before": before, "after": after, "claims_changed": True})
    if omit_summary and sections.get("SUMMARY"):
        del sections["SUMMARY"]
        parsed["section_order"].remove("SUMMARY")
        changes.append({"section": "SUMMARY", "operation": "omit_optional_summary", "claims_changed": False})
    if not sections.get("PROJECTS") and supplemental_text and (relevance_terms or project_titles):
        sourced = parse_entries(parse_resume(supplemental_text)["sections"].get("PROJECTS", ""))
        terms = [term.casefold() for term in relevance_terms or [] if len(term) >= 3]
        scored = [(sum(term in " ".join([entry["title"], *entry["bullets"]]).casefold()
                       for term in terms), i, entry) for i, entry in enumerate(sourced)]
        selected = [entry for score, _, entry in sorted(scored, key=lambda item: (-item[0], item[1]))[:2]
                    if score >= 2 and entry["bullets"]]
        if project_titles:
            selected = [entry for entry in sourced if entry["title"].split("\t")[0] in project_titles]
            if {entry["title"].split("\t")[0] for entry in selected} != set(project_titles):
                raise ValueError("Requested project lacks an exact source entry")
        if selected:
            sections["PROJECTS"] = "\n\n".join(
                "\n".join([entry["title"].split("\t")[0], entry["subtitle"].replace("\t", " | "),
                           *["- " + bullet for bullet in entry["bullets"]]]) for entry in selected
            )
            added_bullets = [bullet for entry in selected for bullet in entry["bullets"]]
            education_index = (parsed["section_order"].index("EDUCATION")
                               if "EDUCATION" in parsed["section_order"] else len(parsed["section_order"]))
            parsed["section_order"].insert(education_index, "PROJECTS")
            changes.append({"section": "PROJECTS", "operation": "add_sourced_projects",
                            "source_titles": [entry["title"] for entry in selected],
                            "claims_changed": False, "source": "supplemental_text"})
    for section in ("EXPERIENCE", "PROJECTS"):
        if section not in sections:
            continue
        entries = parse_entries(sections[section])
        refreshed = False
        if section == "PROJECTS" and refresh_project_titles:
            sourced = parse_entries(parse_resume(supplemental_text)["sections"].get("PROJECTS", ""))
            for title in refresh_project_titles:
                matches = [i for i, entry in enumerate(entries) if entry["title"] == title]
                source_entry = next((entry for entry in sourced if entry["title"].split("\t")[0] == title), None)
                if not matches or not source_entry:
                    raise ValueError("Project refresh requires an existing entry and an exact source entry")
                removed_bullets.extend(b for i in matches for b in entries[i]["bullets"])
                added_bullets.extend(source_entry["bullets"])
                replacement = {**source_entry, "title": title,
                               "subtitle": source_entry["subtitle"].replace("\t", " | ")}
                entries = [replacement if i == matches[0] else entry
                           for i, entry in enumerate(entries) if i == matches[0] or i not in matches]
                refreshed = True
                changes.append({"section": "PROJECTS", "operation": "consolidate_from_source",
                                "source_title": title, "replaced_entries": len(matches), "claims_changed": True})
        if not entries or any(not entry["bullets"] for entry in entries):
            continue
        if section == "EXPERIENCE":
            ordered = order_entries_by_recency(entries)
        else:
            terms = [term.casefold() for term in relevance_terms or [] if len(term) >= 3]
            ordered = sorted(entries, key=lambda entry: -sum(
                term in " ".join([entry["title"], *entry["bullets"]]).casefold() for term in terms
            ))
        if ordered != entries or refreshed:
            sections[section] = "\n\n".join(
                "\n".join([entry["title"], entry["subtitle"], *["- " + bullet for bullet in entry["bullets"]]])
                for entry in ordered
            )
            changes.append({"section": section, "operation": "reorder", "claims_changed": False})
    education = sections.get("EDUCATION", "")
    separated = re.sub(r";\s*(?=(?:University|Nanyang)\b)", "\n", education)
    if separated != education:
        sections["EDUCATION"] = separated
        changes.append({"section": "EDUCATION", "operation": "split_school_lines", "claims_changed": False})
    header = text.split(original_first_section, 1)[0].strip().splitlines()
    if parsed["contact"] and len(header) > 2 and header[1].strip() != parsed["contact"]:
        header = [parsed["name"], parsed["contact"]]
        changes.append({"section": "HEADER", "operation": "contact_after_name", "claims_changed": False})
    if not changes:
        return {"text": text, "changes": [], "claims_preserved": True}
    result = "\n\n".join([
        "\n".join(header),
        *[section + "\n" + sections[section] for section in parsed["section_order"]],
    ]) + "\n"
    # Full bullet equality is stronger than a model's description of its edits.
    bullets = lambda value: Counter(re.findall(r"(?m)^\s*[-•]\s+(.+)$", value))
    if bullets(text) - Counter(removed_bullets) + Counter(added_bullets) != bullets(result):
        raise ValueError("Editorial refinement changed a factual bullet")
    return {"text": result, "changes": changes, "claims_preserved": not bool(summary_replacements or refresh_project_titles)}
