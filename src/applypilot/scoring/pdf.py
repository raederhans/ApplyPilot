"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import logging
import re
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


def canonical_section_header(value: str) -> str | None:
    """Share accepted section aliases between parsing and source ingestion."""
    header = " ".join(value.upper().split())
    if header in SECTION_HEADERS:
        return header
    for pattern, canonical in (
        (r"(?:(?:WORK|PROFESSIONAL|RELEVANT|SELECTED|KEY|EMPLOYMENT) )?(?:EXPERIENCE|HISTORY)", "EXPERIENCE"),
        (r"(?:SELECTED|KEY|PERSONAL|ACADEMIC|RELEVANT) PROJECTS", "PROJECTS"),
        (r"ACADEMIC BACKGROUND|EDUCATION BACKGROUND", "EDUCATION"),
        (r"CORE SKILLS|SKILLS|TECH STACK|TECHNOLOGIES", "TECHNICAL SKILLS"),
        (r"(?:PROFESSIONAL|CAREER|EXECUTIVE) SUMMARY", "SUMMARY"),
    ):
        if re.fullmatch(pattern, header):
            return canonical
    return None


SUMMARY_TAIL_MIN_WORDS = 5
SKILL_TAIL_MIN_WORDS = 5


def _highlight_metrics(text: str) -> str:
    """Bold compact numeric evidence without changing the extracted text."""
    return re.sub(
        r"(?<![A-Za-z0-9])(~?\d[\d,.]*(?:%|\+|x)?(?:-\d[\d,.]*(?:%|\+|x)?)?)",
        r'<strong class="metric">\1</strong>',
        text,
    )


def _format_bullet(text: str, *, emphasize_lead: bool) -> str:
    """Escape a bullet and add one restrained visual anchor when requested."""
    cleaned = str(text).strip()
    if not cleaned:
        return ""
    if not emphasize_lead:
        return _highlight_metrics(escape(cleaned))

    clause_match = re.match(r"^(.{1,78}?)(?=[,;:]\s)", cleaned)
    lead = clause_match.group(1) if clause_match else ""
    if not 3 <= len(re.findall(r"\b[\w+#./-]+\b", lead)) <= 10:
        word_matches = list(re.finditer(r"\S+", cleaned))
        if len(word_matches) >= 4:
            lead = cleaned[: word_matches[3].end()]
        else:
            lead = cleaned
    remainder = cleaned[len(lead) :]
    return (
        f'<strong class="bullet-lead">{escape(lead)}</strong>'
        + _highlight_metrics(escape(remainder))
    )


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


def _pdf_page_text_spans(pdf_path: str | Path) -> list[float]:
    """Measure the vertical text span on each rendered PDF page."""
    from pypdf import PdfReader

    spans: list[float] = []
    for page in PdfReader(str(pdf_path)).pages:
        y_positions: list[float] = []

        def collect_text_position(text, cm, tm, _font, _font_size, positions=y_positions,
                                  bottom=float(page.mediabox.bottom), top=float(page.mediabox.top)) -> None:
            # Chromium uses a translated/flipped matrix for each printed page.
            # pypdf may also flush a synthesized string after resetting tm to
            # identity; that has no usable text baseline and inflated old spans.
            if text.strip() and (tm[4] or tm[5]):
                y = float(tm[4] * cm[1] + tm[5] * cm[3] + cm[5])
                if bottom <= y <= top:
                    positions.append(y)

        page.extract_text(visitor_text=collect_text_position)
        spans.append(
            max(y_positions) - min(y_positions) if len(y_positions) >= 2 else 0.0
        )
    return spans


def _last_page_is_usefully_filled(
    page_text_spans: list[float], min_ratio: float = 0.4
) -> bool:
    """Allow multiple pages only when the final page is materially occupied."""
    if len(page_text_spans) <= 1:
        return True
    reference_span = max(page_text_spans[:-1], default=0.0)
    if reference_span <= 0:
        return False
    return page_text_spans[-1] / reference_span >= min_ratio


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
        if canonical_section_header(line):
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
        header = canonical_section_header(stripped)
        if header:
            if current_section:
                sections[current_section] = "\n\n".join(filter(None, (
                    sections.get(current_section, ""), "\n".join(current_lines).strip(),
                )))
            current_section = header
            if header not in section_order:
                section_order.append(header)
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n\n".join(filter(None, (
            sections.get(current_section, ""), "\n".join(current_lines).strip(),
        )))

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
        elif current:
            current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


# ── HTML Template ────────────────────────────────────────────────────────

def _subtitle_html(value: str) -> str:
    if not value:
        return ""
    parts = value.rsplit(" | ", 1)
    if len(parts) == 2 and re.search(r"\b20\d{2}\b", parts[1]):
        return ('<div class="entry-subtitle"><span>' + escape(parts[0])
                + '</span><span class="entry-date">' + escape(parts[1]) + '</span></div>')
    return f'<div class="entry-subtitle">{escape(value)}</div>'


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
            rows += (
                f'<div class="skill-row"><span class="skill-cat">{escape(cat)}:</span> '
                f'{escape(val)}</div>\n'
            )
        skills_html = f'<div class="section"><div class="section-title">Technical Skills</div>{rows}</div>'

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(
                f'<li>{_format_bullet(bullet, emphasize_lead=index == 0)}</li>'
                for index, bullet in enumerate(e["bullets"])
            )
            subtitle = _subtitle_html(e["subtitle"])
            items += (
                f'<div class="entry"><div class="entry-title">{escape(e["title"])}</div>'
                f"{subtitle}<ul>{bullets}</ul></div>"
            )
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    if "PROJECTS" in sections:
        entries = parse_entries(sections["PROJECTS"])
        items = ""
        for e in entries:
            bullets = "".join(
                f'<li>{_format_bullet(bullet, emphasize_lead=index == 0)}</li>'
                for index, bullet in enumerate(e["bullets"])
            )
            subtitle = _subtitle_html(e["subtitle"])
            items += (
                f'<div class="entry"><div class="entry-title">{escape(e["title"])}</div>'
                f"{subtitle}<ul>{bullets}</ul></div>"
            )
        proj_html = f'<div class="section"><div class="section-title">Projects</div>{items}</div>'

    # Education
    edu_html = ""
    if "EDUCATION" in sections:
        education_rows: list[str] = []
        for line in sections["EDUCATION"].splitlines():
            line = line.strip()
            if not line:
                continue
            school, separator, detail = line.partition(",")
            if separator:
                education_rows.append(
                    '<div class="edu-entry"><span class="edu-school">'
                    f"{escape(school.strip())}</span>, {escape(detail.strip())}</div>"
                )
            else:
                education_rows.append(
                    f'<div class="edu-entry"><span class="edu-school">{escape(line)}</span></div>'
                )
        edu_html = (
            '<div class="section"><div class="section-title">Education</div>'
            f'<div class="edu">{"".join(education_rows)}</div></div>'
        )

    # Summary
    summary_html = ""
    if "SUMMARY" in sections:
        summary_html = (
            '<div class="section"><div class="section-title">Summary</div>'
            f'<div class="summary">{escape(sections["SUMMARY"].strip())}</div></div>'
        )

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
    contact_parts = [escape(p.strip()) for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(contact_parts)

    # Location line (may be empty)
    location_html = (
        f'<div class="location">{escape(resume["location"])}</div>'
        if resume["location"]
        else ""
    )
    title_html = (
        f'<div class="title">{escape(resume["title"])}</div>' if resume["title"] else ""
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.5in;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.35;
    color: #111827;
}}
.header {{
    text-align: center;
    margin-bottom: 3px;
    padding-bottom: 3px;
    border-bottom: none;
}}
.name {{
    font-size: 18pt;
    font-weight: 700;
    color: #111111;
    letter-spacing: 0.35px;
}}
.title {{
    font-size: 10.5pt;
    color: #374151;
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
    margin-top: 4px;
}}
.section-title {{
    font-size: 11.5pt;
    font-weight: 700;
    color: #111111;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 0.6px solid #111;
    padding-bottom: 1px;
    margin-bottom: 3px;
    break-after: avoid;
}}
.summary {{
    font-size: 10.5pt;
    color: #111111;
    line-height: 1.35;
    text-wrap: balance;
}}
.skill-row {{
    font-size: 10.5pt;
    margin: 0;
    line-height: 1.35;
    text-wrap: balance;
}}
.skill-cat {{
    font-weight: 700;
    color: #111111;
}}
.entry {{
    margin-bottom: 3px;
    break-inside: avoid;
}}
.entry-title {{
    break-after: avoid;
    font-weight: 700;
    font-size: 10.5pt;
    color: #111111;
}}
.entry-subtitle {{
    break-after: avoid;
    display: flex;
    justify-content: space-between;
    gap: 8px;
    font-size: 10.5pt;
    color: #374151;
    font-style: italic;
    margin-bottom: 1px;
}}
.entry-date {{
    font-style: normal;
    white-space: nowrap;
}}
.bullet-lead,
.metric {{
    font-weight: 700;
    color: #111111;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    break-inside: avoid;
    font-size: 10.5pt;
    margin-bottom: 0.5px;
    line-height: 1.35;
}}
.edu {{
    font-size: 10.5pt;
}}
.edu-entry {{
    line-height: 1.2;
    margin-bottom: 1px;
    text-wrap: balance;
}}
.edu-entry:last-child {{
    margin-bottom: 0;
}}
.edu-school {{
    font-weight: 700;
    color: #111111;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{escape(resume['name'])}</div>
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
    last_page_min_fill_ratio: float = 0.4,
    one_page_min_fill_ratio: float = 0.78,
    _allow_compact_retry: bool = True,
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
        ) if 'class="summary"' in html else []
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
        content_fill_ratio = float(page.evaluate(
            """() => {
                const nodes = Array.from(document.body.querySelectorAll('.header, .section'));
                if (!nodes.length) return 0;
                const top = Math.min(...nodes.map(node => node.getBoundingClientRect().top));
                const bottom = Math.max(...nodes.map(node => node.getBoundingClientRect().bottom));
                const printableHeight = 11 * 96 - 2 * 0.45 * 96;
                return Math.max(0, (bottom - top) / printableHeight);
            }"""
        ))
        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()
        page_spans = _pdf_page_text_spans(output_path)
        if len(page_spans) == 1 and content_fill_ratio < one_page_min_fill_ratio:
            Path(output_path).unlink(missing_ok=True)
            raise ValueError(
                "Sparse one-page PDF: rendered content fill ratio "
                f"{content_fill_ratio:.0%} is below the configured "
                f"{one_page_min_fill_ratio:.0%}. Retain or strengthen more role-relevant evidence."
            )
        if not _last_page_is_usefully_filled(
            page_spans, min_ratio=last_page_min_fill_ratio
        ):
            Path(output_path).unlink(missing_ok=True)
            fill_ratio = page_spans[-1] / max(page_spans[:-1])
            if _allow_compact_retry:
                compact_override = """
<style>
@page { margin: 0.5in; }
body { font-size: 10pt; line-height: 1.25; }
.header { margin-bottom: 2px; padding-bottom: 2px; }
.name { font-size: 17pt; }
.section { margin-top: 3px; }
.section-title { font-size: 10pt; margin-bottom: 2px; }
.summary { font-size: 10pt; line-height: 1.25; }
.skill-row { font-size: 10pt; line-height: 1.25; }
.entry { margin-bottom: 2px; }
.entry-title { font-size: 10pt; }
.entry-subtitle { font-size: 10pt; margin-bottom: 0; }
li { font-size: 10pt; line-height: 1.25; margin-bottom: 0.5px; }
.edu { font-size: 10pt; }
</style>
"""
                compact_html = html.replace("</head>", compact_override + "</head>")
                compact_browser = p.chromium.launch()
                compact_page = compact_browser.new_page(
                    viewport={"width": 816, "height": 1056}
                )
                compact_page.emulate_media(media="print")
                compact_page.set_content(compact_html, wait_until="networkidle")
                compact_fill_ratio = float(compact_page.evaluate(
                    """() => {
                        const nodes = Array.from(document.body.querySelectorAll('.header, .section'));
                        if (!nodes.length) return 0;
                        const top = Math.min(...nodes.map(node => node.getBoundingClientRect().top));
                        const bottom = Math.max(...nodes.map(node => node.getBoundingClientRect().bottom));
                        const printableHeight = 11 * 96 - 2 * 0.5 * 96;
                        return Math.max(0, (bottom - top) / printableHeight);
                    }"""
                ))
                compact_page.pdf(
                    path=output_path,
                    format="Letter",
                    margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                    print_background=True,
                )
                compact_browser.close()
                compact_spans = _pdf_page_text_spans(output_path)
                if len(compact_spans) == 1:
                    if compact_fill_ratio >= one_page_min_fill_ratio:
                        return
                elif _last_page_is_usefully_filled(
                    compact_spans, min_ratio=last_page_min_fill_ratio
                ):
                    return
                Path(output_path).unlink(missing_ok=True)
            raise ValueError(
                "Sparse final PDF page: rendered fill ratio "
                f"{fill_ratio:.0%} is below the configured "
                f"{last_page_min_fill_ratio:.0%}. Use one page or retain enough "
                "distinct role-relevant evidence to justify another page."
            )


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path,
    output_path: Path | None = None,
    html_only: bool = False,
    layout_override: dict | None = None,
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
    layout = (
        dict(layout_override)
        if layout_override is not None
        else load_profile().get("tailoring", {}).get("resume_layout", {})
    )
    summary_tail_min_words = int(
        layout.get("summary_min_rendered_tail_words", SUMMARY_TAIL_MIN_WORDS)
        or SUMMARY_TAIL_MIN_WORDS
    )
    skill_tail_min_words = int(
        layout.get("technical_skill_min_rendered_tail_words", SKILL_TAIL_MIN_WORDS)
        or SKILL_TAIL_MIN_WORDS
    )
    last_page_min_fill_ratio = float(
        layout.get("multi_page_last_page_min_fill_ratio", 0.4) or 0.4
    )
    one_page_min_fill_ratio = float(
        layout.get("one_page_min_fill_ratio", 0.78) or 0.78
    )
    render_pdf(
        html,
        str(out),
        summary_tail_min_words=summary_tail_min_words,
        skill_tail_min_words=skill_tail_min_words,
        last_page_min_fill_ratio=last_page_min_fill_ratio,
        one_page_min_fill_ratio=one_page_min_fill_ratio,
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
