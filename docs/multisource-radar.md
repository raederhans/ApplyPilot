# ApplyPilot 多来源求职雷达

这套雷达把“发现职位”从原有固定求职网站扩展为四层信息面：

1. 公司官网和官方 ATS/RSS：可核验的正式职位，经过新加坡地域和现有标题过滤后进入 `jobs`；赛道标签缺失保留待评估。
2. LinkedIn Content Search：生成候选人可见的定向查询 URL；ApplyPilot 不自动抓取 LinkedIn。
3. 新加坡校园、政府和行业门户：由候选人在场复核后只进入 `radar_leads`；导入时不依据门户声明或历史职位自动升级。
4. SGInnovate/Startup SG 公司目录：只进入 `radar_company_seeds`，用于后续发现和核验公司官网招聘入口，不虚构职位。

```text
official careers / ATS / RSS ──> source run ──> observation ──> verified job
LinkedIn / forum / community ──> source run ──> observation ──> lead
                                                        │
                                      official-open verification
                                                        ▼
                                                   verified job
ecosystem company directory ──> source run ──> company seed
                                                        │
                                      official-careers verification
                                                        ▼
                                              watchlist candidate
```

## P2 新加坡生态来源

- CareerAxis、MyCareersFuture、Careers@Gov Early Careers：仅支持有人在场的 URL 导入，覆盖状态固定为 `non_exhaustive`。
- SGInnovate Deep Tech Central：公开公司目录可导入 company seed；具体岗位仍须人工确认后以 lead 导入。
- Startup SG Directory：仅支持 company seed，不接受职位记录。
- Singapore FinTech Association Job Portal：截至 2026-08-28 公共入口不可用，注册为 disabled；恢复前不会接受导入，也不会把不可达折算为零结果。

所有 portal lead 均强制写成 `awaiting_official + unverified`。即便导入文件自行声称 `promoted`、`verified_official` 或“官方发布者”，也不会绕过核验；只有本轮新鲜官方来源精确观察到相同雇主 ATS URL 后才可升级。

## 赛道结构

顶层继续保持四条稳定赛道，产品经理、售前和规划作为子赛道扩展：

- `general_product_consulting`：产品管理、产品运营、战略运营、售前/解决方案、实施咨询。
- `data_bi_decision`：数据分析、BI、业务运营分析、规划分析、战略分析。
- `ai_implementation`：AI 解决方案、Forward Deployed AI、工作流自动化、AI 产品运营、AI 技术售前。
- `spatial`：城市规划、交通规划、地理空间、区位智能、数字孪生、城市科技。

官网职位使用与 LinkedIn 查询相同的职位词表进行保守标题分类。未匹配到目标子赛道的全球职位不会进入正式雷达结果。

## 当前官网覆盖

默认每日启用 22 个官方来源：

- Greenhouse：Databricks、Cloudflare、Stripe、Anthropic、MongoDB、Datadog、Temus、StraitsX、Workato、SimplifyNext、Geotab、Shift Technology。
- Ashby：OpenAI、Venti Technologies、Simular、k-ID。
- Lever：ShopBack、Portcast、GoTo Group。
- SmartRecruiters：Grab（列表分页后读取同源官方详情；总数、offset 和岗位 ID 不守恒时记为 `partial`）。
- Workable：Porsche Asia Pacific（读取公开 account jobs 集合，并按 URL 路径区分职位页和申请页）。
- 官方 XML/RSS：ST Engineering。
- ST Engineering 仅公开最新条目，固定记录为 `partial`，不能用空结果宣称“官网零职位”。
- Palantir、Wise 等已验证但当前无新加坡岗位的来源保留为 inactive，可在 dry-run 中检查，不能直接 live 采集。

## 常用命令

在 `applypilot-local` 目录使用安全包装器：

```powershell
.\run-radar.ps1 sync-linkedin-applied --file .\data\radar-imports\linkedin-applied-YYYY-MM-DD.json
.\run-radar.ps1 radar collect
.\run-radar.ps1 radar collect --company openai --company grab
.\run-radar.ps1 radar collect --company shopback --company venti_technologies --company porsche_asia_pacific
.\run-radar.ps1 radar collect --dry-run --include-inactive

.\run-radar.ps1 radar queries --track ai_implementation --window past-24h
.\run-radar.ps1 radar queries --subtrack product_management --window past-week
.\run-radar.ps1 radar queries --subtrack transport_planning --window past-month

.\run-radar.ps1 -AttendedReview radar import-leads --file .\data\radar-imports\linkedin-leads-YYYY-MM-DD.json
.\run-radar.ps1 -AttendedReview radar import-leads --source-id careeraxis --file .\data\radar-imports\careeraxis-leads-YYYY-MM-DD.json
.\run-radar.ps1 -AttendedReview radar import-leads --source-id mycareersfuture --file .\data\radar-imports\mcf-leads-YYYY-MM-DD.json
.\run-radar.ps1 -AttendedReview radar import-company-seeds --source-id startup-sg-directory --file .\data\radar-imports\startup-sg-companies-YYYY-MM-DD.json
.\run-radar.ps1 radar report --hours 24 --require-applied-snapshot <sync 返回的 snapshot_id> --output .\data\reports\daily-radar-YYYY-MM-DD.md
```

LinkedIn Applied 同步支持两种明确语义：

- `sync_mode: "full"`（默认）是翻完所有当前可见页的基线/校验快照。只有 `complete=true`、`observed_total`与输入记录数一致、无重复 job ID、无 skipped且观察时间带时区时，才能用于日报的完整性 gate。
- `sync_mode: "incremental"` 是基于一个已通过完整性校验的 `base_snapshot_id` 追加新 Applied job ID。它会立即更新累计历史排重集，但不会伪装成当前 LinkedIn 全量覆盖，因此不能单独满足 `--require-applied-snapshot`。

增量文件的最小格式：

```json
{
  "source": "linkedin_job_tracker_incremental_read",
  "sync_mode": "incremental",
  "base_snapshot_id": "<a complete snapshot_id>",
  "observed_at": "2026-08-31T09:00:00+08:00",
  "observed_total": 74,
  "pages_read": 1,
  "applications": [
    {"url": "https://www.linkedin.com/jobs/view/123456789/"}
  ]
}
```

实际排重账本仍是本地 `jobs` 表：完整基线、后续增量和 ApplyPilot 自身已接纳的 receipt 都会合并进这个只增不减的排除集。由于 LinkedIn 页面没有仓库内可验证的官方 cursor，快速增量不能证明“没有漏掉其他新记录”；证据型日报或分页/计数异常时仍必须做周期性完整校验。

`run-radar.ps1` 是运行时能力白名单：只放行 Applied 同步和五个 radar 子命令，限制导入/报告路径和扩展，并在启动 Python 前拒绝 apply、pipeline、tailor、cover 等入口。两类导入都要求 `-AttendedReview`；source ID 必须出现在各自 allowlist，disabled 来源会在 Python registry 再次拒绝。日报必须绑定同一次同步返回的完整 Applied snapshot；快照观察时间超过 6 小时、计数不守恒、存在 skipped 或 ID 不匹配时，不会创建日报。

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
# Bounded employer exploration (September 2026)

The daily radar now has three complementary paths: the existing official
watchlist, cross-company LinkedIn/Indeed searches, and advancement of imported
company/role leads. A source registry entry alone never starts discovery.

From the local installation directory:

```powershell
.\run-radar.ps1 radar collect
.\run-radar.ps1 radar explore --limit 5
.\run-radar.ps1 radar explore --query "business analyst" --job-type internship --limit 5
.\run-radar.ps1 radar advance --limit 5
```

`explore` defaults to two role queries rotating through the four fields, both
platforms, and five retained leads per query/platform. Search fetches at most
twice that number (capped at ten), then rotates employers before truncating.
An agent may choose up to three
queries and ten results, broaden a sparse field, or inspect a directory instead.
These bounds limit effort; they do not assign employer quotas or change fit
scores. Retain useful large-company monitoring and use a final shortlist of
5–10 suitable jobs. Prefer unprocessed employers among equally suitable roles.

Each board runs independently with one 30-second attempt. `partial`, `empty`
and `error` describe that query; none implies exhaustive platform coverage.
Company/title/source/description and employer targets retain their provenance.
Missing company metadata is explicitly returned for review. Board results create
unverified `radar_leads`, never verified `jobs` directly. Portal destinations
such as MyCareersFuture are not silently treated as employer careers URLs.

Use the returned `search_url` in the existing visible browser session when the
API gives missing metadata, noisy/empty results or an access error. Read a small
set of actual job cards, verify the selected location/type/date filters, then
read job duties and the official link. Browser-visible review by the authorized
agent can supply a JSON/CSV file to `run-radar.ps1 -AttendedReview radar
import-leads --source-id linkedin-jobs` (or `indeed-jobs`); it does not require a
human to review each record. Stop at CAPTCHA/security challenges. Social-content
URL generation remains separate from Jobs search and is non-exhaustive.

In the September 5 bounded live comparison, both APIs returned three records.
LinkedIn's public search still returned experienced roles after an internship
filter; the signed-in visible search showed four different internship results
and confirmed its selected filters. Therefore HTTP success is not evidence that
LinkedIn honored filters. Indeed's installed adapter cannot combine `hours_old`
with `job_type`; explicit `--job-type` drops its time filter and records that
limitation. The agent should verify dates on returned pages.

For smaller employers and organizations, inspect a few CareerAxis/SGInnovate/
Startup SG entries when a field is sparse, retain the directory URL, employer
name and actual careers URL, and import via the existing source-specific command.
`advance` consumes both role leads and company seeds with a shared small budget.
It returns missing-link items separately with an explicit next action. Public
JSON-LD verification can admit jobs through the existing official ingestion and
fresh exact-URL reconciliation contract. Pages without usable structured job
data remain pending for visible review or a supported official adapter; this is
not a general-purpose ATS crawler. Seeing a directory entry is never a verified
job or an application receipt.

For a board lead, the company name and employer URL originate in the same
untrusted result. Before promotion, independently inspect the employer identity
in the visible browser, then use `-AttendedReview radar import-leads
--official-targets-reviewed` for that reviewed file. The CLI issues a 24-hour
exact-target attestation after normalization; source-supplied trust fields are
discarded. A redirect to a different host requires a new review. This step can
be performed by the authorized agent, and does not require per-job user approval.

The same-score diversity preference also applies to batch authorization and
worker acquisition. Recent means an actual attempt or application within 14
days. Higher fit scores remain ahead, exact user-selected URLs remain exact,
and no employer is excluded. A title without a known radar subtrack now remains
available for duties-based assessment instead of being discarded. Existing
location, admission, submission and Applied-snapshot checks still apply.

The operational `applypilot-local/run-radar.ps1` is outside this repository.
Deployments must allow `explore` and `advance` in its discovery command list and
`linkedin-jobs`/`indeed-jobs` in its reviewed lead sources. It must still block
application commands and constrain imports/reports to the workspace directories.
