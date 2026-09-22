# Job Apply Pilot — 本地优先、结果可核验的求职申请自动化

<p align="center">
  <img src="docs/brand/job-apply-pilot/assets/readme-hero.svg" alt="Job Apply Pilot：发现、判断、准备、核验" width="100%">
</p>

[![Release](https://img.shields.io/github/v/release/raederhans/AutoJobApply?label=Job%20Apply%20Pilot&color=175CD3)](https://github.com/raederhans/AutoJobApply/releases/latest)
[![CI](https://github.com/raederhans/AutoJobApply/actions/workflows/ci.yml/badge.svg)](https://github.com/raederhans/AutoJobApply/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11–3.13-0B1F3A)](pyproject.toml)
[![License](https://img.shields.io/badge/license-AGPL--3.0-20B486)](LICENSE)

[English](README.md) | [简体中文](README.zh-CN.md)

**找到真实职位，用证据做选择，准备可信材料，在你的控制下投递，并证明究竟提交了什么。**

Job Apply Pilot 是覆盖完整求职申请闭环的本地工作台。它把官方来源职位发现、资格与匹配判断、基于证据的简历路由、忠于事实的材料定制、受监督的浏览器协助、申请历史和回执核验放进同一套可审计流程。

它服务于“提高效率，但不交出判断权”的求职者。个人资料、简历、凭据、浏览器会话、回执和运行日志默认都留在本机。

## 它具体能做什么

| 需求 | Job Apply Pilot 的能力 | 证据边界 |
| --- | --- | --- |
| 找到机会 | 收集官方职位、可选招聘平台结果和人工审核线索 | 搜索结果不自动等于已核验职位 |
| 判断是否值得投 | 检查资格、补全职位描述、评估匹配度、安排申请优先级 | 高分不等于已经获得投递授权 |
| 生成申请材料 | 路由已验证简历、识别事实缺口、不编造经历地定制内容、校验 PDF | AI 生成内容不自动视为可信 |
| 完成申请表 | 在明确授权后准备并填写受支持的申请页面 | CAPTCHA、MFA、测评、敏感文件和无依据问题会停下复核 |
| 确认真实结果 | 将具体职位、授权、浏览器观察和回执绑定到同一次尝试 | 点击 **Submit** 不等于雇主已经收到 |

## 工作流

```text
发现 DISCOVER             判断 DECIDE               准备 PREPARE              核验 VERIFY
官方职位           →      资格检查            →      简历路由           →      具体职位回执
平台/人工线索             匹配证据                   可信定制                  持久申请历史
来源状态                  准备程度                   PDF + 表单准备             不盲目重投
```

受控申请链路是：

```text
准备 → 审核 → 授权 → 提交 → 观察 → 核验回执
```

如果无法证明平台已经接收申请，该尝试会保留为 `submission_uncertain`。Job Apply Pilot 不会把它悄悄算作成功，也不会自动重复提交同一个申请。

## 它为什么不是原版 ApplyPilot

本仓库是 [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) 的独立延续项目，但产品契约已经明确不同。

| | 原版 ApplyPilot | Job Apply Pilot |
| --- | --- | --- |
| 产品承诺 | 高并发、全自动海量投递 | 以证据为基础、受监督的申请执行 |
| 成功标准 | 浏览器完成提交动作 | 获得具体职位回执，或明确保留不确定状态 |
| 人的角色 | 尽量无人值守 | 在关键边界复核与授权 |
| 数据模型 | 流水线输出 | 持久记录来源、材料、授权、尝试与回执 |
| 隐私模式 | 本地开源代理 | 无托管账户、无云同步的本地优先工作台 |
| 工作台 | 申请进度 | 只读证据界面，不暗中写库或提交 |

对外产品名是 **Job Apply Pilot**。为了兼容已有用户，技术标识保持不变：CLI 与 Python 包仍为 `applypilot`，发行包仍为 `applypilot-local`，环境变量仍使用 `APPLYPILOT_*`，默认工作区仍为 `~/.applypilot/`。

本项目与 applypilot.app、useapplypilot.com 或其他近似名称的服务没有关联。

## 当前状态

- **Beta、本地优先、以 CLI 为主。** 目前没有 Job Apply Pilot 托管服务、在线账户、遥测看板或云同步。
- **端到端流程已经可用。** 当前代码支持发现、补全、评分、简历库路由、忠于事实的定制、求职信与 PDF 准备、受监督的申请协助、历史记录和回执核验。
- **浏览器工作台只读。** 它呈现“发现、判断、准备、核验”四个阶段，不会修改数据库或执行命令。
- **人工复核是功能，不是缺陷。** 无事实依据的回答、CAPTCHA 或 MFA、测评、身份或财务文件、账户恢复、安全设置变化和不确定结果都会成为明确的交接点。
- **当前版本为 v0.6.0。** 本次发布不改变原有命令、本地数据和兼容标识。

## 安装

完整工作流推荐 Python 3.11 或 3.12。核心命令和官方来源职位雷达也支持 Python 3.13。

PyPI 暂无 `applypilot-local` 发行包。请从[最新 GitHub Release](https://github.com/raederhans/AutoJobApply/releases)安装，或直接安装当前仓库：

```bash
pipx install "git+https://github.com/raederhans/AutoJobApply.git@v0.6.0"
```

需要可复现的本地部署时，请下载 release bundle 与 `SHA256SUMS`，校验后解压并运行：

```bash
python install.py
```

可选的第三方招聘平台发现功能目前建议在 Python 3.11–3.12 使用：

```bash
python install.py --with-jobboards
```

## 快速开始

初始化工作区、检查能力并打开只读工作台：

```bash
applypilot init
applypilot doctor
applypilot dashboard
```

发现职位并准备材料：

```bash
applypilot radar collect
applypilot radar report --hours 24
applypilot run discover enrich score tailor cover pdf
```

对一个具体职位进行提交前审核：

```bash
applypilot review-readiness
applypilot apply --dry-run --url <verified-job-url>
applypilot authorize-batch --url <verified-job-url>
applypilot apply --authorization-file <batch-manifest.json>
applypilot reconcile-receipts --file <receipt.json>
```

检查并路由已验证的简历版本：

```bash
applypilot resume-library-sync
applypilot resume-library-status
applypilot resume-library-review
applypilot resume-route --url <verified-job-url>
```

运行 `applypilot --help` 查看完整命令列表。

## 安全与本地数据

不要提交或分享本地工作区。其中可能包含个人资料、简历、生成文档、SQLite 数据库、API 密钥、浏览器配置、截图、回执、日志或验证码。

Job Apply Pilot 不支持绕过 CAPTCHA、隐藏提交、自动处理身份证明文件或自动恢复账户。报告安全问题前请阅读 [SECURITY.md](SECURITY.md)。

## 开发

```bash
python -m pip install -e ".[dev]"
ruff check src
pytest -q
python scripts/build_release.py
```

- [产品与前端边界](docs/product-core.md)
- [简历库整理说明](docs/resume-library-curation.md)
- [公司优先级策略](docs/company-priority.md)
- [更新日志](CHANGELOG.md)
- [贡献指南](CONTRIBUTING.md)
- [许可证与来源](NOTICE.md)

Job Apply Pilot 使用 [GNU AGPL-3.0-only](LICENSE) 许可证。原版 ApplyPilot 作者继续保留其上游代码的版权。
