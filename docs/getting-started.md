# From setup to your first application

[English README](../README.md) · [中文 README](../README.zh-CN.md)

## Installation options

Use Python 3.11 or 3.12 for the complete workflow. Core commands and the official
radar also support Python 3.13. With `pipx` installed:

```bash
pipx install "git+https://github.com/raederhans/AutoJobApply.git@v0.7.0"
applypilot init
applypilot doctor
```

Alternatively, download a bundle from [Releases](https://github.com/raederhans/AutoJobApply/releases),
extract it, and run `python install.py`. To include third-party job-board search
on Python 3.11–3.12, use `python install.py --with-jobboards`.

The setup wizard collects your resume, profile, search preferences, and optional
AI configuration. Gemini, OpenAI, and compatible local endpoints are supported.
Browser application work uses your authenticated Codex CLI (default) or Claude CLI
and an installed Chromium browser. Follow `doctor`'s component-specific output.

Your default workspace is `~/.applypilot/`. To use another directory, put
`--workspace <directory>` **before** each command:

```bash
applypilot --workspace ./my-job-search init
applypilot --workspace ./my-job-search dashboard
```

Already have your files? Import all three together:

```bash
applypilot init --resume resume.txt --profile profile.json --searches searches.yaml
```

## Discover, compare, and prepare

```bash
applypilot radar collect
applypilot run enrich score
applypilot dashboard
```

In **Decide**, search by role or company, set a minimum fit score, and inspect the
description and matching reasons. **Discover** shows the sources behind the queue.
The default radar is Singapore-oriented; customize `searches.yaml` / `radar.yaml`
for your market. See [radar configuration](multisource-radar.md).

Prepare materials for eligible queued jobs:

```bash
applypilot run tailor cover pdf
applypilot dashboard
```

Review the generated files and the **Prepare** tab. An existing resume library can
be synced with `applypilot resume-library-sync` and inspected with
`applypilot resume-library-status`; route one job with
`applypilot resume-route --url "<job-url>"`.

## Review and submit one job

Replace the example URL below with the exact prepared job. Review the job's
requirements, your availability, work authorization, and application materials.
Record your own review reason; the quoted reason below is a template to replace.

```bash
applypilot review-readiness --url "https://employer.example/jobs/123" --status confirmed --reason "Replace with your job-specific review findings" --reviewed-by user
applypilot apply --dry-run --url "https://employer.example/jobs/123"
```

If something still needs checking, record `--status needs_review` with the
outstanding question. If a successful browser preview confirms that the form
has no cover-letter field, use
`applypilot mark-cover-not-required --url "<job-url>" --verified-by user`.

When the preview and materials are ready:

```bash
applypilot authorize-batch --url "https://employer.example/jobs/123" --output first-application.json
applypilot apply --authorization-file "<manifest-path-printed-by-authorize-batch>" --limit 1
applypilot status
applypilot dashboard
```

The authorization command checks preparation and prints the manifest's actual
path under your workspace's `application-batches/` directory. Use that printed
path in the next command. If your workspace has a grouped final-authorization
policy, follow the additional instructions printed by the CLI.

Use **Verify** to review application outcomes and follow-ups. For an outstanding
receipt, `applypilot reconcile-receipts --file "<receipt.json>"` imports the
matching receipt envelope. Run `applypilot <command> --help` for options.

## Working with Codex's in-app browser

An attended Codex session can connect the existing browser worker to an in-app
tab, prepare an application, and review its result. Start with the
[browser worker guide](visual-worker-bridge.md). For several roles, use the
[batch preparation guide](attended-runtime-batch.md); see the
[published runtime observations](attended-runtime-results.md) for exercised paths.

## 中文操作索引

1. **首次配置**：安装后运行 `init`，按向导提供简历、资料和求职偏好，再用 `doctor` 查看组件状态。
2. **找岗与筛选**：运行 `radar collect`、`run enrich score` 和 `dashboard`，在“判断”页选择岗位。
3. **准备材料**：运行 `run tailor cover pdf`，检查生成文件与“准备”页；这些阶段作用于符合条件的队列。
4. **审核与预览**：按上面的完整 `review-readiness` 命令填写具体岗位的审核结果，然后执行 `apply --dry-run --url`。
5. **授权与投递**：运行 `authorize-batch`，把它输出的实际文件路径交给 `apply --authorization-file`，用 `--limit 1` 从一个岗位开始。
6. **查看结果**：使用 `status` 和工作台“核验”页；运行 `dashboard` 可重新生成最新视图。

命令中的示例 URL、审核理由和文件路径都需要替换成自己的真实值。默认工作区为 `~/.applypilot/`，自定义目录使用全局参数 `--workspace`。
