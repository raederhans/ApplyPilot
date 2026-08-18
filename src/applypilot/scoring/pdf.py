"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import logging
from html import escape
from pathlib import Path

from applypilot.config import TAILORED_DIR, load_profile

log = logging.getLogger(__name__)

SECTION_HEADERS = {
    "SUMMARY",
    "TECHNICAL SKILLS",
    "EXPERIENCE",
    "PROJECTS",
    "EDUCATION",
}
SUMMARY_TAIL_MIN_WORDS = 5
SKILL_TAIL_MIN_WORDS = 5


def _tail_is_dense(line_word_counts: list[int], min_words: int) -> bool:
    """Return whether a wrapped text block avoids an underfilled tail line."""
    return len(line_word_counts) <= 1 or line_word_counts[-1] >= min_words


def _summary_tail_is_dense(
    line_word_counts: list[int], min_words: int = SUMMARY_TAIL_MIN_WORDS
) -> bool:
    """Return whether a wrapped summary avoids an orphaned tail line."""
    return _tail_is_dense(line_word_counts, min_words)


def _skill_tails_are_dense(
    skill_line_word_counts: list[list[int]], min_words: int = SKILL_TAIL_MIN_WORDS
) -> bool:
    """Return whether every rendered Technical Skills row has a useful tail line."""
    return all(_tail_is_dense(counts, min_words) for counts in skill_line_word_counts)


# ── Resume Parser ────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a format with header lines (name, optional title/location, contact)
    followed by ALL-CAPS section headers (SUMMARY, TECHNICAL SKILLS, etc.).

    Args:
        text: Full resume text.

    Returns:
        A parsed resume including the source section order.
    """
    lines = [line.rstrip() for line in text.strip().split("\n")]

    # Header: non-empty lines before the first recognized section.
    header_lines: list[str] = []
    body_start = len(lines)
    for i, line in enumerate(lines):
        if line.strip().upper() in SECTION_HEADERS:
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())

    name = header_lines[0] if header_lines else ""
    title = ""
    location = ""
    contact = ""
    for header_line in header_lines[1:]:
        is_contact = "@" in header_line or "|" in header_line
        if is_contact:
            contact = header_line
        elif not title:
            title = header_line
        elif not location:
            location = header_line

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    section_order: list[str] = []
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        # Detect section headers (all caps, no leading dash/bullet, longer than 3 chars)
        if (
            stripped
            and stripped == stripped.upper()
            and not stripped.startswith("-")
            and len(stripped) > 3
            and not stripped.startswith("\u2022")
        ):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            section_order.append(stripped)
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
        "section_order": section_order,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs.

    Args:
        text: The TECHNICAL SKILLS section text.

    Returns:
        List of (category_name, skills_string) tuples.
    """
    skills: list[tuple[str, str]] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text.

    Args:
        text: The EXPERIENCE or PROJECTS section text.

    Returns:
        List of {"title": str, "subtitle": str, "bullets": list[str]} dicts.
    """
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("- ", "\u2022 ")):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif current is None or (
            not stripped.startswith("-")
            and not stripped.startswith("\u2022")
            and len(current.get("bullets", [])) > 0
        ):
            # New entry
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


# ── HTML Template ────────────────────────────────────────────────────────

def build_html(resume: dict) -> str:
    """Build professional resume HTML from parsed data.

    Args:
        resume: Parsed resume dict from parse_resume().

    Returns:
        Complete HTML string ready for PDF rendering.
    """
    sections = resume["sections"]

    # Skills
    skills_html = ""
    if "TECHNICAL SKILLS" in sections:
        skills = parse_skills(sections["TECHNICAL SKILLS"])
        rows = ""
        for cat, val in skills:
            rows += f'<div class="skill-row"><span class="skill-cat">{cat}:</span> {val}</div>\n'
        skills_html = f'<div class="section"><div class="section-title">Technical Skills</div>{rows}</div>'

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    if "PROJECTS" in sections:
        entries = parse_entries(sections["PROJECTS"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        proj_html = f'<div class="section"><div class="section-title">Projects</div>{items}</div>'

    # Education
    edu_html = ""
    if "EDUCATION" in sections:
        edu_text = "<br>".join(
            escape(line.strip())
            for line in sections["EDUCATION"].splitlines()
            if line.strip()
        )
        edu_html = f'<div class="section"><div class="section-title">Education</div><div class="edu">{edu_text}</div></div>'

    # Summary
    summary_html = ""
    if "SUMMARY" in sections:
        summary_html = f'<div class="section"><div class="section-title">Summary</div><div class="summary">{sections["SUMMARY"].strip()}</div></div>'

    section_html = {
        "SUMMARY": summary_html,
        "TECHNICAL SKILLS": skills_html,
        "EXPERIENCE": exp_html,
        "PROJECTS": proj_html,
        "EDUCATION": edu_html,
    }
    fallback_order = ["SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"]
    requested_order = resume.get("section_order") or fallback_order
    rendered_sections = "\n".join(
        section_html[name] for name in requested_order if section_html.get(name)
    )

    # Contact line parsing
    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(contact_parts)

    # Location line (may be empty)
    location_html = f'<div class="location">{resume["location"]}</div>' if resume["location"] else ""
    title_html = f'<div class="title">{resume["title"]}</div>' if resume["title"] else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.35in 0.5in;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.35;
    color: #1a1a1a;
}}
.header {{
    text-align: center;
    margin-bottom: 4px;
    padding-bottom: 4px;
    border-bottom: 1.5px solid #2a7ab5;
}}
.name {{
    font-size: 18pt;
    font-weight: 700;
    color: #1a3a5c;
    letter-spacing: 0.5px;
}}
.title {{
    font-size: 10.5pt;
    color: #3a6b8c;
    margin: 1px 0;
}}
.location {{
    font-size: 9pt;
    color: #555;
}}
.contact {{
    font-size: 9pt;
    color: #444;
    margin-top: 1px;
}}
.contact a {{
    color: #2c3e50;
    text-decoration: none;
}}
.section {{
    margin-top: 5px;
}}
.section-title {{
    font-size: 10pt;
    font-weight: 700;
    color: #1a3a5c;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1.5px solid #2a7ab5;
    padding-bottom: 1px;
    margin-bottom: 3px;
}}
.summary {{
    font-size: 9.5pt;
    color: #333;
    line-height: 1.4;
    text-wrap: pretty;
}}
.skill-row {{
    font-size: 9.5pt;
    margin: 0;
    line-height: 1.35;
    text-wrap: pretty;
}}
.skill-cat {{
    font-weight: 600;
    color: #1a3a5c;
}}
.entry {{
    margin-bottom: 4px;
    break-inside: avoid;
}}
.entry-title {{
    font-weight: 600;
    font-size: 10pt;
    color: #1a3a5c;
}}
.entry-subtitle {{
    font-size: 9pt;
    color: #4a7a9b;
    font-style: italic;
    margin-bottom: 1px;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    font-size: 9.5pt;
    margin-bottom: 1px;
    line-height: 1.35;
}}
.edu {{
    font-size: 10pt;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{resume['name']}</div>
    {title_html}
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{rendered_sections}
</body>
</html>"""


# ── PDF Renderer ─────────────────────────────────────────────────────────

def render_pdf(
    html: str,
    output_path: str,
    summary_tail_min_words: int = SUMMARY_TAIL_MIN_WORDS,
    skill_tail_min_words: int = SKILL_TAIL_MIN_WORDS,
) -> None:
    """Render HTML to PDF using Playwright's headless Chromium.

    Args:
        html: Complete HTML string.
        output_path: Path to write the PDF file.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 816, "height": 1056})
        page.emulate_media(media="print")
        page.set_content(html, wait_until="networkidle")
        summary_line_word_counts = page.eval_on_selector(
            ".summary",
            """el => {
                const node = el.firstChild;
                if (!node) return [];
                const lines = new Map();
                for (const match of node.textContent.matchAll(/\\S+/g)) {
                    const range = document.createRange();
                    range.setStart(node, match.index);
                    range.setEnd(node, match.index + match[0].length);
                    const top = Math.round(range.getBoundingClientRect().top);
                    lines.set(top, (lines.get(top) || 0) + 1);
                }
                return Array.from(lines.values());
            }""",
        )
        if not _summary_tail_is_dense(summary_line_word_counts, summary_tail_min_words):
            browser.close()
            raise ValueError(
                "Summary has an underfilled rendered tail line: "
                f"{summary_line_word_counts[-1]} words; minimum {summary_tail_min_words}."
            )
        skill_rows = page.eval_on_selector_all(
            ".skill-row",
            """elements => elements.map((el, index) => {
                const lines = new Map();
                const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    for (const match of node.textContent.matchAll(/\\S+/g)) {
                        const range = document.createRange();
                        range.setStart(node, match.index);
                        range.setEnd(node, match.index + match[0].length);
                        const top = Math.round(range.getBoundingClientRect().top);
                        lines.set(top, (lines.get(top) || 0) + 1);
                    }
                }
                return {
                    index,
                    lineWordCounts: Array.from(lines.values()),
                    text: el.textContent.trim(),
                };
            })""",
        )
        skill_line_word_counts = [row["lineWordCounts"] for row in skill_rows]
        if not _skill_tails_are_dense(skill_line_word_counts, skill_tail_min_words):
            failed_row = next(
                row
                for row in skill_rows
                if not _tail_is_dense(row["lineWordCounts"], skill_tail_min_words)
            )
            browser.close()
            raise ValueError(
                "Technical Skills row "
                f"{failed_row['index'] + 1} has an underfilled rendered tail line: "
                f"{failed_row['lineWordCounts'][-1]} words; minimum "
                f"{skill_tail_min_words}."
            )
        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False
) -> Path:
    """Convert a text resume/cover letter to PDF.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    resume = parse_resume(text)
    html = build_html(resume)

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    layout = load_profile().get("tailoring", {}).get("resume_layout", {})
    summary_tail_min_words = int(
        layout.get("summary_min_rendered_tail_words", SUMMARY_TAIL_MIN_WORDS)
        or SUMMARY_TAIL_MIN_WORDS
    )
    skill_tail_min_words = int(
        layout.get("technical_skill_min_rendered_tail_words", SKILL_TAIL_MIN_WORDS)
        or SKILL_TAIL_MIN_WORDS
    )
    render_pdf(
        html,
        str(out),
        summary_tail_min_words=summary_tail_min_words,
        skill_tail_min_words=skill_tail_min_words,
    )
    log.info("PDF generated: %s", out)
    return out


def batch_convert(limit: int = 50) -> int:
    """Convert .txt files in TAILORED_DIR that don't have corresponding PDFs.

    Scans for .txt files (excluding _JOB.txt and _REPORT.json), checks if a
    .pdf with the same stem already exists, and converts any that are missing.

    Args:
        limit: Maximum number of files to convert.

    Returns:
        Number of PDFs generated.
    """
    if not TAILORED_DIR.exists():
        log.warning("Tailored directory does not exist: %s", TAILORED_DIR)
        return 0

    txt_files = sorted(TAILORED_DIR.glob("*.txt"))
    # Exclude _JOB.txt and _CL.txt files from resume conversion
    # (they get their own conversion calls)
    candidates = [
        f for f in txt_files
        if not f.name.endswith("_JOB.txt")
    ]

    # Filter to those without a corresponding PDF
    to_convert: list[Path] = []
    for f in candidates:
        pdf_path = f.with_suffix(".pdf")
        if not pdf_path.exists():
            to_convert.append(f)
        if len(to_convert) >= limit:
            break

    if not to_convert:
        log.info("All text files already have PDFs.")
        return 0

    log.info("Converting %d files to PDF...", len(to_convert))
    converted = 0
    for f in to_convert:
        try:
            convert_to_pdf(f)
            converted += 1
        except Exception as e:  # noqa: BLE001 - one bad artifact must not stop the batch
            log.error("Failed to convert %s: %s", f.name, e)

    log.info("Done: %d/%d PDFs generated in %s", converted, len(to_convert), TAILORED_DIR)
    return converted
