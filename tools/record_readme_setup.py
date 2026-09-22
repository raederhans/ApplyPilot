"""Record the real setup wizard and build a public-data walkthrough workspace.

This documentation tool uses fictional applicant facts and a dated snapshot of
two public vacancies. It never runs discovery, scoring, or a live application.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
RESUME = """Alex Morgan
Data Analyst | Fictional demonstration profile
demo@example.test | Singapore

SUMMARY
Data analyst with practical experience in SQL, Python and clear reporting. Builds reproducible analysis, checks data quality and explains findings to stakeholders. Interested in product analytics, useful metrics and reliable workflows that help teams understand customer behavior and make informed decisions.

TECHNICAL SKILLS
Languages: Python, SQL, JavaScript
Analytics: pandas, NumPy, exploratory analysis, cohort analysis, data quality
Tools: Git, Jupyter, PostgreSQL, Excel, Power BI

PROJECTS
Customer Analytics Dashboard | Demonstration Project | 2026
- Built a SQL reporting model for sample orders, customer activity and weekly product metrics, with explicit definitions for each measure.
- Used Python to check missing identifiers, duplicate transactions and unexpected date ranges before preparing weekly summaries for review.
- Created an interactive dashboard that lets a reader compare product segments, inspect trends and trace a chart back to the underlying data.
- Documented assumptions and explained how changes in customer behavior could affect reported conversion and retention metrics.

Data Quality Toolkit | Demonstration Project | 2026
- Developed reusable checks for sample CSV and database inputs, including schema validation, null counts, duplicate detection and range constraints.
- Organized validation results by dataset and severity so analysts could investigate specific failures without repeating the entire workflow.
- Added small regression examples for common input errors and documented how to run the checks from a command line.
- Compared reports before and after cleaning, preserving raw inputs and recording the reason for each transformation.

Research Reporting Workflow | Demonstration Project | 2025
- Combined public sample datasets into a consistent research table using Python and SQL, keeping original source links with every imported record.
- Built reproducible notebooks that separate data preparation, exploration and final reporting to make review and revision straightforward.
- Prepared concise presentations of methods, findings and limitations for readers with different levels of technical experience.
- Collaborated on review checklists covering units, time periods, missing observations and the meaning of derived indicators.

Product Metrics Exploration | Demonstration Project | 2025
- Analyzed a fictional product event dataset to understand sign-up, activation and repeat-use patterns across customer cohorts.
- Defined events and conversion windows before computing results, and compared alternative definitions to explain sensitivity to assumptions.
- Produced readable charts and short explanations that connect data patterns to specific questions a product team could investigate.
- Maintained a clean project repository containing notebooks, example inputs, a data dictionary and instructions for reproducing the analysis.

EDUCATION
Example University | BSc in Data Analytics | 2023 - 2026
Coursework: Statistics, databases, programming, research methods and data visualization.
This resume contains fictional demonstration facts only.
"""

JOBS = [
    {
        "url": "https://jobs.lever.co/shopback-2/b216d68c-48b0-4fa5-8f1e-9e0375b993e1",
        "title": "Data Analyst (Internship) (H1 2027)",
        "company_name": "ShopBack",
        "location": "Singapore",
        "fit_score": 9,
        "score_reasoning": "Sample profile review: SQL, Python and dashboard projects align with the role.",
        "full_description": "Public vacancy snapshot, 22 Sep 2026. Six-month full-time internship in Singapore. "
        "Responsibilities include metrics and dashboards, data models, AI-assisted analytics and data quality checks. "
        "SQL is required; Python is useful. Applicants should be located in Singapore. "
        "Open the source page to read the full current requirements.",
    },
    {
        "url": "https://jobs.lever.co/portcast/f18cc64e-c34a-416c-b213-62a39906260e",
        "title": "Data Analyst Intern",
        "company_name": "Portcast",
        "location": "Singapore / Remote",
        "fit_score": 8,
        "score_reasoning": "Sample profile review: Python, SQL and data-quality projects are relevant.",
        "full_description": "Public vacancy snapshot, 22 Sep 2026. Work with the data team on source integration, "
        "quality checks, reporting and automation. Python and SQL knowledge are relevant. "
        "The role lists Singapore among its remote locations. Open the source for current requirements.",
    },
]


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="jap-motion-"))
    work = root / "workspace"
    site = root / "site"
    site.mkdir()
    resume = root / "demo-resume.txt"
    resume.write_text(RESUME, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "APPLYPILOT_DIR": str(work),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "NO_COLOR": "1",
        "COLUMNS": "95",
    }
    events, chunks = [], []
    started = time.monotonic()
    # Windows getpass otherwise reads the host console instead of redirected stdin.
    # Only the input transport changes; the wizard itself runs unmodified.
    launcher = "import getpass,runpy; getpass.getpass=getpass.fallback_getpass; runpy.run_module('applypilot.cli',run_name='__main__')"
    process = subprocess.Popen(
        [sys.executable, "-c", launcher, "--workspace", str(work), "init"],
        cwd=root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=0,
    )

    def read_output() -> None:
        while character := process.stdout.read(1):
            chunks.append(character)
            events.append({"t": time.monotonic() - started, "kind": "output", "text": character})

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    answers = [
        ("Resume file path", str(resume)),
        ("Full name", "Alex Morgan"),
        ("Preferred/nickname", "Alex"),
        ("Email address", "demo@example.test"),
        ("Phone number", ""),
        ("City", "Singapore"),
        ("Province/State", ""),
        ("Country", "Singapore"),
        ("Postal/ZIP", ""),
        ("Street address", ""),
        ("LinkedIn URL", ""),
        ("GitHub URL", ""),
        ("Portfolio URL", ""),
        ("Personal website", ""),
        ("Job site password", ""),
        ("legally authorized", "y"),
        ("need sponsorship", "n"),
        ("Work permit type", ""),
        ("Expected annual salary", ""),
        ("Currency", "SGD"),
        ("Acceptable range", ""),
        ("Current/most recent", "Data Analyst"),
        ("Target role", "Data Analyst Intern"),
        ("Years of professional", "0"),
        ("Highest education", "Bachelor's"),
        ("Programming languages", "Python, SQL"),
        ("Frameworks & libraries", "pandas"),
        ("Tools & platforms", "Git, Jupyter, Power BI"),
        ("Companies to always keep", ""),
        ("Projects to always keep", "Customer Analytics Dashboard"),
        ("School name(s)", "Example University"),
        ("Real metrics to preserve", ""),
        ("Earliest start date", "January 2027"),
        ("Target location", "Singapore"),
        ("Search radius", "0"),
        ("Target job titles", "Data Analyst, Product Analyst"),
        ("Enable AI scoring", "n"),
        ("Enable autonomous job", "n"),
    ]
    offset = 0
    try:
        for prompt, value in answers:
            deadline = time.monotonic() + 15
            while prompt not in "".join(chunks)[offset:]:
                if time.monotonic() > deadline or process.poll() is not None:
                    raise RuntimeError(f"Wizard did not reach {prompt!r}: {''.join(chunks)[-500:]}")
                time.sleep(0.02)
            offset = len(chunks)
            time.sleep(0.16)
            for character in value:
                process.stdin.write(character)
                process.stdin.flush()
                events.append({"t": time.monotonic() - started, "kind": "input", "text": character})
                time.sleep(0.018)
            process.stdin.write("\n")
            process.stdin.flush()
            events.append({"t": time.monotonic() - started, "kind": "input", "text": "\n"})
        if process.wait(timeout=20) != 0:
            raise RuntimeError("Setup wizard failed")
        reader.join(timeout=2)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
    # Replace only the local root in the recorded stream; preserve CLI output.
    transcript = "".join(event["text"] for event in events)
    transcript = ANSI.sub("", transcript).replace(str(root), "<demo>")
    (root / "wizard-transcript.txt").write_text(transcript, encoding="utf-8")
    (root / "wizard-events.json").write_text(json.dumps(events), encoding="utf-8")
    os.environ["APPLYPILOT_DIR"] = str(work)
    sys.path.insert(0, str(ROOT / "src"))
    from applypilot.database import close_connection, init_db
    from applypilot.scoring.pdf import convert_to_pdf

    connection = init_db(work / "applypilot.db")
    materials = work / "tailored_resumes"
    materials.mkdir(exist_ok=True)
    material = materials / "Alex_Morgan_Data_Analyst.txt"
    material.write_text(RESUME, encoding="utf-8")
    convert_to_pdf(material, site / "resume.html", html_only=True)
    for job in JOBS:
        row = {
            **job,
            "application_url": job["url"] + "/apply",
            "site": "Lever",
            "source_site": "Public careers page",
            "eligibility_status": "eligible",
            "tailored_resume_path": str(material),
            "tailor_status": "draft",
        }
        connection.execute(
            "INSERT INTO jobs (" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")", tuple(row.values())
        )
    connection.commit()
    close_connection(work / "applypilot.db")
    for command in (["dashboard", "--no-open", "--output", str(site / "dashboard.html")], ["run", "pdf"]):
        completed = subprocess.run(
            [sys.executable, "-m", "applypilot.cli", "--workspace", str(work), *command],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        (root / (command[0] + "-output.txt")).write_text(completed.stdout + completed.stderr, encoding="utf-8")
    pdf = material.with_suffix(".pdf")
    if not pdf.exists():
        raise RuntimeError(f"PDF generation failed: {(root / 'run-output.txt').read_text(encoding='utf-8')}")
    shutil.copy2(pdf, site / "Alex_Morgan_Data_Analyst.pdf")
    subprocess.run(["pdftoppm", "-png", "-r", "120", "-singlefile", str(pdf), str(site / "resume-page")], check=True)
    build_material_pages(root, site)
    # Keep only selected stretches of the real wizard for a readable short replay.
    # Each scene begins at an observed prompt; the renderer reveals the real
    # captured output and recorded input progressively, never inventing CLI status.
    scenes = []
    for start_text, end_text, en, zh in [
        ("Step 1: Resume", "Phone number", "Import your resume and profile", "导入简历，填写基本资料"),
        (
            "Step 3: Job Search Config",
            "Step 4: AI Features",
            "Choose your market and target roles",
            "设置求职地区与目标岗位",
        ),
        ("Step 4: AI Features", "Step 5: Auto-Apply", "Configure AI when you are ready", "按需配置 AI 服务"),
        ("Setup complete!", None, "Your workspace is ready", "工作区初始化完成"),
    ]:
        begin = transcript.index(start_text)
        end = transcript.index(end_text, begin) if end_text else len(transcript)
        excerpt = transcript[begin:end].strip()
        excerpt = "\n".join(
            line.rstrip(" │") for line in excerpt.splitlines() if not any(mark in line for mark in ("─", "┘", "└"))
        )
        scenes.append({"text": excerpt, "en": en, "zh": zh})
    for lang in ("en", "zh"):
        page = PLAYER.replace("__LANG__", lang).replace("__SCENES__", json.dumps(scenes).replace("<", "\\u003c"))
        (site / f"setup-{lang}.html").write_text(page, encoding="utf-8")
    print(f"DEMO_ROOT={root}")
    print(f"SITE={site}")
    print(f"Wizard exit=0; PDF={pdf.stat().st_size} bytes; public roles={len(JOBS)}")


def build_material_pages(root: Path, site: Path) -> None:
    log = (root / "run-output.txt").read_text(encoding="utf-8")
    log = log[log.index("Converting 1 files") :].replace(str(root), "<demo>")
    for lang, heading, action, back in [
        ("en", "Your PDF is ready to inspect", "Open resume preview", "Back to CLI output"),
        ("zh", "PDF 已生成，打开检查排版", "打开简历预览", "返回 CLI 输出"),
    ]:
        style = "body{margin:0;background:#0b1f3a;color:white;font:25px/1.6 system-ui}main{padding:50px}h1{font-size:38px;color:#80dfcc}pre{white-space:pre-wrap;font:22px/1.7 Consolas,monospace;background:#18314e;padding:25px}a{display:inline-block;padding:14px 26px;background:#175cd3;color:white;border-radius:6px;text-decoration:none}"
        page = f'<!doctype html><meta charset="utf-8"><title>Job Apply Pilot — Recorded PDF output</title><style>{style}</style><main><p>JOB APPLY PILOT / CLI</p><h1>{heading}</h1><pre>$ applypilot run pdf\n\n{html.escape(log)}</pre><a href="resume-{lang}.html">{action} →</a></main>'
        (site / f"materials-{lang}.html").write_text(page, encoding="utf-8")
        preview = f'<!doctype html><meta charset="utf-8"><title>Alex Morgan — PDF preview</title><style>body{{margin:0;background:#e8eef6;font:18px system-ui}}header{{padding:16px 32px;background:#0b1f3a;color:white}}a{{color:#80dfcc}}main{{height:780px;overflow:auto}}img{{display:block;width:1020px;max-width:95%;margin:25px auto;box-shadow:0 8px 40px #0b1f3a33}}</style><header>Alex_Morgan_Data_Analyst.pdf · <a href="materials-{lang}.html">{back}</a></header><main><img src="resume-page.png" alt="Rendered resume PDF"></main>'
        (site / f"resume-{lang}.html").write_text(preview, encoding="utf-8")


PLAYER = """<!doctype html><meta charset="utf-8"><title>Job Apply Pilot — Setup recording</title>
<style>body{margin:0;background:#0b1f3a;color:#e8eef6;font:25px/1.5 Consolas,monospace}
header{padding:18px 32px;background:#18314e;display:flex;justify-content:space-between;font:16px system-ui}
main{padding:22px 40px}h1{font:30px system-ui;color:#80dfcc}pre{white-space:pre-wrap;font:inherit;max-height:650px;overflow:hidden}
button{background:#175cd3;color:white;border:0;padding:10px 22px;font:16px system-ui;border-radius:6px;cursor:pointer}
#prompt{color:#80dfcc}.caret{display:inline-block;background:#80dfcc;width:10px;height:22px;animation:blink 1s infinite}
@keyframes blink{50%{opacity:0}}</style><header><span>JOB APPLY PILOT / CLI</span>
<button id="play"></button></header><main><h1 id="heading"></h1><p id="prompt">$ applypilot init</p>
<pre><span id="output"></span><span class="caret"></span></pre></main><script>
const lang='__LANG__',scenes=__SCENES__;const play=document.querySelector('#play');
play.textContent=lang==='zh'?'播放配置过程':'Play setup recording';
document.querySelector('#heading').textContent=lang==='zh'?'一次配置，开始求职':'Set up once. Start your search.';
play.onclick=async()=>{play.disabled=true;for(const scene of scenes){document.querySelector('#heading').textContent=scene[lang];
const output=document.querySelector('#output');output.textContent='';
for(let i=0;i<scene.text.length;i+=7){output.textContent=scene.text.slice(0,i+7);output.parentElement.scrollTop=output.parentElement.scrollHeight;await new Promise(r=>setTimeout(r,24));}
await new Promise(r=>setTimeout(r,1400));}play.disabled=false;};</script>"""


if __name__ == "__main__":
    main()
