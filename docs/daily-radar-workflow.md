# Daily radar automation instructions

This is the reviewed workflow for the existing local daily radar automation.
The schedule and model are managed by Codex; this file records its discovery
scope and operating instructions.

每天执行“新加坡四赛道职位雷达”。目标是用小预算发现适合申请的岗位和新公司，形成5–10个候选；保留有价值的大公司监控，按证据判断，不设公司配额。本自动化只发现和核验，不申请、填表、发消息、上传或定制简历，不创建提交授权清单，不点击任何最终提交控件。

主目录 C:\Users\raede\Desktop\简历；运行目录 C:\Users\raede\Desktop\简历\applypilot-local。先阅读 source\AGENTS.md 和 source\docs\multisource-radar.md。只能通过 run-radar.ps1 调用 ApplyPilot，不得使用 run.ps1、applypilot.exe、Python 模块入口或投递命令。只写 automation memory、data\radar-imports、data\reports 和雷达数据库，不改变申请状态、浏览器账户或用户资料。按当前已确认的本地资料进行资格判断，禁止沿用旧提示中脱离当前资料的统一16小时/必须学分规则；未知材料不得编造。

1. 使用已登录 LinkedIn 的可见 My Jobs / Job Tracker 完整读取 Applied，逐页到末页，提取公司、职位、地点、URL/job id，核对页面总数、唯一记录数和页数。登录、验证码、权限、分页或计数任何一项不完整就结束并报告“无推荐：无法验证已申请职位排除集”，不写推荐历史。不得把部分读取冒充完整。
2. 完整时保存 data\radar-imports\linkedin-applied-YYYY-MM-DD.json，包含 source、complete:true、带时区 observed_at、observed_total、pages_read、applications。通过 .\run-radar.ps1 sync-linkedin-applied --file <该文件> 同步；只有 completeness=complete、skipped=0、inserted+updated=declared_total 时继续，保存本次 snapshot_id。
3. 运行 .\run-radar.ps1 radar collect，保留逐来源健康与覆盖。接着运行 .\run-radar.ps1 radar explore --limit 5；默认两个轮换的岗位查询分别搜索 LinkedIn 与 Indeed，也可按本轮缺口选择一至三个简单查询。保留 General/Product-Consulting、Data/BI/Decision、AI Implementation/Solutions/Automation、Spatial Data/Urban Tech 四赛道，可按职责接纳混合和相邻岗位，不因标题没有标签而否决。
4. 两个平台都检查独立 search_status、公司/职责/官网链接完整性；empty 只代表该查询，error 不等于没有岗位。需要实习时可选 --job-type internship，遇到噪声可改一个查询或拓宽时间。Indeed 使用该类型选项时没有日期过滤，逐条核对日期。LinkedIn 公开接口可能忽略过滤器，即使成功也须在已有浏览器会话打开返回的 search_url，用可见的地点、职位类型、时间筛选和实际卡片核对一小页。仅在必要时读详情，观察官方申请入口；不启用提醒，不保存职位，不提交申请。每站至多一次有界接口尝试及一次可见页面补查；停止于验证码或安全挑战。
5. 至少考虑一次固定观察名单外的雇主探索。如果求职网站已带来新的合适公司，可直接推进；若仍集中在熟悉公司或某领域稀疏，从 CareerAxis、SGInnovate 或 Startup SG 可见目录中选择最多三家相关公司/组织，核查实际官网与招聘页。不强求“startup”关键词，不因为公司规模而放宽岗位质量。已授权的 Agent 可以独立进行可见页面复核；将有来源证据的职位/公司保存为导入文件，通过 .\run-radar.ps1 -AttendedReview radar import-leads --source-id linkedin-jobs（或 indeed-jobs/careeraxis/其他已启用来源）或 import-company-seeds --source-id sginnovate-dtc（或 startup-sg-directory）导入。该标记只用于本轮确已完成的来源复核，不能虚构人工检查。
6. 运行 .\run-radar.ps1 radar advance --limit 5。使用 attempts/unresolved 中的 source_url、target_url、原因和 next_action 推进缺失链接；不支持的动态页面可由 Agent 可见复核或交已有官方适配器。目录、搜索线索和无结构数据页不得直接当作已验证岗位。新公司经核验后按有界队列继续刷新，失败不视为“没有招聘”。不为凑数重复搜索同一家公司。
7. 使用同一 snapshot_id 运行 .\run-radar.ps1 radar report --hours 24 --require-applied-snapshot <snapshot_id> --output .\data\reports\daily-radar-YYYY-MM-DD.md；任何快照、时效、计数或命令错误都停止发布推荐。排除已申请、确实重复、关闭的职位；历史推荐可供避免重复展示，但新 requisition 可按事实判断。
8. 使用当前1–10评分体系和既有6分准入阈值，不生成另一套0–100评分，不调整分数。已有分数按证据解释；尚未评分标“待评分”，不得假称已过阈值。结合完整职责、可迁移经历和本地已确认事实区分硬冲突与可补足差距，保留Agent判断，不用孤立关键词否决。仅在同样适合时优先近期未处理公司；大公司出现新的高匹配岗位仍可入选。
9. 输出来源健康及各站实际覆盖、本次 Applied snapshot、实际新公司/待核验数量，再列“可继续准备”“待核验/待评分”和简短排除原因。每个候选给赛道、1–3条事实依据、缺口、来源URL及已核验的官方URL。默认总计5–10个，宁可不足也不降低质量。只有实际展示的已验证岗位才写 automation memory，保留日期/公司/职位/地点/URL或job id/来源/去重键；等待核验不等于成功投递。

最多运行20分钟。沿用当前用户资料和门户政策；预算不足时保存明确的待推进项，不把未执行或访问失败报告为零结果。中文、结论先行，明确哪些来源本次尚未覆盖。
