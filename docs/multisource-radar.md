# ApplyPilot 多来源求职雷达

这套雷达把“发现职位”从原有固定求职网站扩展为三层信息面：

1. 公司官网和官方 ATS/RSS：可核验的正式职位，经过新加坡地域、标题和赛道过滤后进入 `jobs`。
2. LinkedIn Content Search：生成候选人可见的定向查询 URL；ApplyPilot 不自动抓取 LinkedIn。
3. LinkedIn 帖子、论坛和社区：只进入 `radar_leads`；导入时不依据历史职位自动升级，只有下一次新鲜官网采集仍看到相同正式链接后才升级。

```text
official careers / ATS / RSS ──> source run ──> observation ──> verified job
LinkedIn / forum / community ──> source run ──> observation ──> lead
                                                        │
                                      official-open verification
                                                        ▼
                                                   verified job
```

## 赛道结构

顶层继续保持四条稳定赛道，产品经理、售前和规划作为子赛道扩展：

- `general_product_consulting`：产品管理、产品运营、战略运营、售前/解决方案、实施咨询。
- `data_bi_decision`：数据分析、BI、业务运营分析、规划分析、战略分析。
- `ai_implementation`：AI 解决方案、Forward Deployed AI、工作流自动化、AI 产品运营、AI 技术售前。
- `spatial`：城市规划、交通规划、地理空间、区位智能、数字孪生、城市科技。

官网职位使用与 LinkedIn 查询相同的职位词表进行保守标题分类。未匹配到目标子赛道的全球职位不会进入正式雷达结果。

## 当前官网覆盖

默认每日启用：Databricks、Cloudflare、Stripe、Grab、OpenAI、Anthropic、MongoDB、Datadog、ST Engineering。

- Greenhouse：Databricks、Cloudflare、Stripe、Anthropic、MongoDB、Datadog。
- Ashby：OpenAI。
- 官方 XML/RSS：Grab、ST Engineering。
- ST Engineering 仅公开最新条目，固定记录为 `partial`，不能用空结果宣称“官网零职位”。
- Palantir、Wise 等已验证但当前无新加坡岗位的来源保留为 inactive，可在 dry-run 中检查，不能直接 live 采集。

## 常用命令

在 `applypilot-local` 目录使用安全包装器：

```powershell
.\run-radar.ps1 sync-linkedin-applied --file .\data\radar-imports\linkedin-applied-YYYY-MM-DD.json
.\run-radar.ps1 radar collect
.\run-radar.ps1 radar collect --company openai --company grab
.\run-radar.ps1 radar collect --dry-run --include-inactive

.\run-radar.ps1 radar queries --track ai_implementation --window past-24h
.\run-radar.ps1 radar queries --subtrack product_management --window past-week
.\run-radar.ps1 radar queries --subtrack transport_planning --window past-month

.\run-radar.ps1 -AttendedReview radar import-leads --file .\data\radar-imports\linkedin-leads-YYYY-MM-DD.json
.\run-radar.ps1 radar report --hours 24 --require-applied-snapshot <sync 返回的 snapshot_id> --output .\data\reports\daily-radar-YYYY-MM-DD.md
```

`run-radar.ps1` 是运行时能力白名单：只放行 Applied 同步和四个 radar 子命令，限制导入/报告路径和扩展，并在启动 Python 前拒绝 apply、pipeline、tailor、cover 等入口。日报必须绑定同一次同步返回的完整 Applied snapshot；快照观察时间超过 6 小时、计数不守恒、存在 skipped 或 ID 不匹配时，不会创建日报。

LinkedIn 默认 prompt 采用实测更能压低全球泛帖噪声的本地招聘标签格式：

```text
#hiring "AI engineer" #singaporejobs
#hiring "product manager" #singaporejobs
#hiring "solution engineer" #singaporejobs
#hiring "transport planner" #singaporejobs
```

时间窗直接编码为 LinkedIn Content Search 的 `datePosted` 参数：`past-24h`、`past-week`、`past-month`；排序固定为 latest。每日无人值守任务只生成待人工复核 URL，不打开或抓取帖子。候选人在场的独立复核中，每条可入库线索仍必须在同一帖子内证明具体职位、Singapore 地点、发布者和可核验的正式链接；泛行业帖、求职帖及仅顺带提到 Singapore 的全球汇总帖全部忽略。复杂 Boolean 查询仍可由纯逻辑层显式生成，但不作为默认值。

## 真值与安全边界

- `complete + 0` 才表示该来源本轮确实没有合格结果。
- `partial`、`blocked` 或 `skipped` 必须显示 unavailable/原因，不能折算为零。
- 通用 `Remote` / `Hybrid` 不证明可以从新加坡工作；默认必须同时出现 Singapore、APAC 等配置地域。
- ATS 的占位 requisition（例如 `See opening ID`）不会用于去重。
- 同一真实 requisition 可以保留多个来源 observation；日报只显示一个正式职位，并标出来源数量和全部 source IDs。
- JSON 分页只跟随 HTTPS 同源链接，并受最大页数限制；异常分页会记录为 `partial`。
- 雷达专用初始化不加载 `.env`，也不创建简历定制、求职信、浏览器 worker 或申请 worker 目录。
- `radar` 子命令不调用 apply、表单填写、消息、简历上传或推荐历史写入；日报明确只是发现证据，不是已发布推荐。

用户覆盖配置位于 `APPLYPILOT_DIR/radar.yaml`。若不存在，会优先复用真实存在的 `searches.yaml`，全新安装则使用包内新加坡雷达默认策略。只有在明确希望人工复核无地域说明的远程职位时，才应在 `radar.yaml` 设置 `allow_ambiguous_remote: true`。
