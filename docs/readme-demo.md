# README recordings

The README has separate English and Chinese recordings for three stages:

| Recording | What it shows |
| --- | --- |
| `workflow-en.gif` / `workflow-zh.gif` | Search the real workbench, expand a role, inspect its public careers page, click Apply, and type into the employer form |
| `setup-en.gif` / `setup-zh.gif` | Selected prompts and answers from a successful `applypilot init` run |
| `materials-en.gif` / `materials-zh.gif` | Inspect the Prepare tab, view real `run pdf` output, open the rendered PDF, and scroll through it |

The workbench and browser segments are continuous screencasts, edited together
at chapter boundaries. Pointer movements follow recorded browser actions; rings
highlight actual clicks. The setup clip replays captured CLI output and input
with a shorter reading pace. The CLI output viewer and PDF preview wrapper are
recording aids, separate from the product GUI.

## Sources and sample data

Recorded on **2026-09-22**, using the v0.7.0-era source checkout:

- [ShopBack — Data Analyst (Internship) (H1 2027)](https://jobs.lever.co/shopback-2/b216d68c-48b0-4fa5-8f1e-9e0375b993e1)
- [Portcast — Data Analyst Intern](https://jobs.lever.co/portcast/f18cc64e-c34a-416c-b213-62a39906260e)

The public vacancies were accessible when recorded. The local workbench uses
short, dated snapshots of those listings. Applicant details, resume projects,
fit scores, and eligibility flags are illustrative data. Scoring and tailoring
were not run against an LLM for this recording. The resume is recorded as a
draft, and the material checklist shows that state.

The attending Codex session operated the actual employer page, typed
`DEMO Applicant` and `demo@example.test`, then cleared both fields and closed
the page. It did not submit an application. This take demonstrates the attended
browser flow; it does not show a separately launched `browser-work` worker.

The Chinese recordings use the Chinese workbench and Chinese captions. Public
employer pages, resume contents, and current CLI prompts retain their original
English text.

## Build the isolated recording workspace

Use a source checkout with the project installed. The generator also needs
Poppler's `pdftoppm` on PATH to render the PDF preview.

```bash
python tools/record_readme_setup.py
```

The generator runs the actual setup wizard, `dashboard`, and `run pdf` in a
fresh temporary workspace. On Windows, the wizard subprocess redirects the
password input transport to stdin; it supplies an empty password. The product
wizard code and validation remain unchanged.

The script prints `DEMO_ROOT` and `SITE`. It creates:

- `workspace/`: a separate profile and SQLite database with two public-role snapshots;
- `wizard-transcript.txt` and `wizard-events.json`: captured prompts, answers, and timing;
- `site/dashboard.html`: the generated product workbench and its assets;
- `site/setup-en.html` and `site/setup-zh.html`: selected CLI transcript playback;
- `site/materials-*.html` and `site/resume-*.html`: command output and a preview of the generated PDF;
- `site/Alex_Morgan_Data_Analyst.pdf`: the actual generated document.

Serve only the generated site directory:

```bash
python -m http.server 8769 --bind 127.0.0.1 --directory "<DEMO_ROOT>/site"
```

## Capture continuous interactions

Read the Browser skill and its CDP capability documentation, then use the
supported browser JavaScript session. Import `createRecorder` from
`tools/readme-recorder.mjs` using an absolute path.

```javascript
const rec = await createRecorder(tab, '<checkout>/output/readme-motion/workbench-en');
await rec.record(async () => {
  rec.chapter('Search your queue and compare the role', '搜索岗位队列，查看匹配依据');
  await rec.pause(1000);
  // Inspect the current screenshot, then use its actual control coordinates.
  await rec.move(searchX, searchY);
  await rec.click();
  await rec.type('ShopBack');
  await rec.pause(1500);
});
```

The recorder saves CDP screencast frames and `recording.json`, including actual
frame timestamps, pointer movement, click events, and bilingual captions.
Actions remain explicitly selected by the attending session.

Record these directories under `output/readme-motion/`:

| Directory | Actions |
| --- | --- |
| `workbench-en`, `workbench-zh` | Select the language, search ShopBack, expand the source description, click the role |
| `live` | Open the public ShopBack listing, scroll its requirements, return to the top, click Apply |
| `form` | Type the marked demo name and email, then scroll to inspect the following fields |
| `setup-en`, `setup-zh` | Open `setup-<language>.html` and play the captured wizard excerpt |
| `materials-en`, `materials-zh` | Open Prepare, expand the first material record, scroll the checklist |
| `resume-en`, `resume-zh` | Open `materials-<language>.html`, click the PDF preview, scroll inside it |

Record only demonstration data. After recording a public form, clear the demo
fields and close the task-owned page. Capture the settled workbench in each
language as `workbench-en.png` and `workbench-zh.png`.

## Encode and verify

Install Pillow in the media environment and put ffmpeg on PATH:

```bash
python tools/encode_readme_demo.py --recordings output/readme-motion --speed 1.5
```

The encoder samples the recordings at 12 fps and plays all frames at 1.5× speed
(18 fps), producing six looping GIFs at 1120 × 712 pixels with language-specific
captions and click indicators. Use `--speed 1` for the original pace. Tall captures use the upper
16:9 region so the README keeps a consistent frame. Review the whole timeline,
including the first frame, transitions, text entry, final state, and caption
readability. Copy the two workbench screenshots into `docs/assets/demo/`.

Raw frames, temporary workspaces, browser logs, and transcripts stay outside the
published assets. The README includes the curated GIFs and screenshots.
