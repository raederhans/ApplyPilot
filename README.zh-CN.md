# CapyPilot

[English](README.md) | [简体中文](README.zh-CN.md)

**一个本地优先、以证据为准的个人求职工作台。**

CapyPilot 帮助求职者发现真实可核验的职位、判断匹配度、准备可信材料、
在明确授权后协助填写受支持的申请表，并确认一次申请究竟有没有真正提交成功。
个人资料、简历、凭据、浏览器会话、回执和运行日志默认都留在用户自己的电脑上。

CapyPilot 面向有监督的执行流程，而不是不加判断地批量海投。点击
**Submit** 不等于申请成功；只有与具体职位准确对应的明确证据，才能形成持久的
已提交记录。

## 当前状态

- **Beta、本地优先、以 CLI 为主。** 产品运行在用户自己的电脑上；目前没有
  CapyPilot 托管服务、在线账户系统或云同步。
- **现阶段已经可以完成端到端流程。** 当前代码支持官方来源职位发现与手动导入、
  匹配度评分、已验证简历复用、基于事实的简历定制、求职信和 PDF 准备、
  授权后的浏览器协助、申请历史记录以及回执核验。
- **浏览器工作台是只读界面。** 它按照“发现、判断、准备、核验”四个阶段呈现
  已有数据，不会修改数据库，也不会直接执行命令。
- **人工复核仍然是产品流程的一部分。** 缺乏事实依据的材料问题、CAPTCHA、
  MFA、在线测评、身份或财务文件、账户恢复、安全设置变更，以及无法确认的
  提交结果，都会停下来交给用户处理。
- **CapyPilot 品牌尚未正式发布。** 当前公开的最新版本仍是使用旧名称
  **ApplyPilot Local** 的 v0.4.0。当前仓库正在完成 CapyPilot 品牌迁移，
  同时保留原有技术标识以维持兼容性。

## 产品流程

| 阶段 | CapyPilot 做什么 | 不会擅自认定什么 |
| --- | --- | --- |
| 发现 Discover | 从官方来源、可选招聘平台和手动线索中收集职位，并保留来源状态 | 一条线索不等于已经核验的职位 |
| 判断 Decide | 检查资格、补充职位描述、评估匹配度并记录准备状态 | 高匹配分数不等于已经获得投递授权 |
| 准备 Prepare | 路由已验证的简历、识别证据缺口、定制内容并验证 PDF | 不会编造缺失的经历或事实 |
| 核验 Verify | 区分授权、浏览器观察、平台状态和可持久保存的回执 | 预览页面或最后一次点击不等于平台已接收申请 |

安全的申请链路是：

```text
准备 -> 审核 -> 授权 -> 提交 -> 观察 -> 核验回执
```

如果无法证明平台已经接收申请，该记录会保留为 `submission_uncertain`，
系统不会自动再次提交。

## 安装

完整工作流推荐使用 Python 3.11 或 3.12。核心命令和官方来源职位雷达也支持
Python 3.13。

目前 PyPI 上还没有发布 `applypilot-local`。请从
[最新 GitHub Release](https://github.com/raederhans/ApplyPilot/releases)
安装，或者直接安装当前仓库：

```bash
pipx install "git+https://github.com/raederhans/ApplyPilot.git"
```

Release 安装包或源码检出中也提供了引导式安装器：

```bash
python install.py
```

第三方招聘平台发现功能是可选组件，目前建议在 Python 3.11–3.12 下使用：

```bash
python install.py --with-jobboards
```

## 快速开始

初始化本地工作区并检查可用能力：

```bash
applypilot init
applypilot doctor
applypilot dashboard
```

发现职位并准备申请材料：

```bash
applypilot radar collect
applypilot radar report --hours 24
applypilot run discover enrich score tailor cover pdf
```

在提交前审核一个明确的职位：

```bash
applypilot review-readiness
applypilot apply --dry-run --url <verified-job-url>
applypilot authorize-batch --url <verified-job-url>
applypilot apply --authorization-file <batch-manifest.json>
applypilot reconcile-receipts --file <receipt.json>
```

也可以单独检查和路由已经验证的简历版本：

```bash
applypilot resume-library-sync
applypilot resume-library-status
applypilot resume-route --url <verified-job-url>
```

运行 `applypilot --help` 可以查看完整命令列表。可选浏览器后端、交互模式、
不同提供方的行为和详细运行规则由命令帮助及产品文档说明，不在本首页展开。

## 兼容性与本地数据

产品对外名称已经改为 **CapyPilot**，但在迁移期间，下列技术标识保持不变：

- distribution：`applypilot-local`
- Python 包与 CLI：`applypilot`
- 环境变量：`APPLYPILOT_*`
- 默认工作区：`~/.applypilot/`
- 数据库结构、存储键、程序入口和仓库地址

不要提交或分享本地工作区。里面可能包含个人资料、简历、生成的申请材料、
SQLite 数据库、API 密钥、浏览器配置、截图、回执、日志或验证码。
CapyPilot 不支持绕过 CAPTCHA、隐藏提交、自动处理身份证明文件或自动恢复账户。
报告安全问题前，请先阅读 [SECURITY.md](SECURITY.md)。

## 开发与文档

```bash
python -m pip install -e ".[dev]"
ruff check src
pytest -q
python scripts/build_release.py
```

- [产品与前端边界](docs/product-core.md)
- [更新日志](CHANGELOG.md)
- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)
- [许可证与项目来源](NOTICE.md)

CapyPilot 使用 [GNU AGPL-3.0-only](LICENSE) 许可证。它是
[Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot)
的独立延续项目；原始作者仍然保留其上游代码的版权。本仓库与
applypilot.app、useapplypilot.com 以及其他同名或近似名称的产品没有关联。
