# 小批次 material admission 对比（合成数据）

日期：2026-09-05。范围限定为 `ad81f1c`（A，旧实现）与 `a7cb610` 加本次局部修复（B）。所有数据库、岗位 URL、简历文字和 PDF 字节均为临时合成数据；没有读取、写入或发送任何候选人资料、账户或投递。

## 结论

B 的实际领取路径没有增加首个合格岗位的材料事实读取：5 岗和 10 岗 cohort 均为 A=1、B=1，且两版都领取同一个首项。B 新增的只读 `next` 投影会为其展示的完整 5/10 岗批次各做 5/10 次事实读取；这是为了在启动前给出与领取一致的候选集，不是 ATS 吞吐或耗时基准。

同时修复了一个材料真实性问题：准入现在通过 `resolve_resume_attachment` 定位实际会上传的 PDF，而不再把 `tailored_resume_path` 的 `.txt` 或同名 sidecar 当作上传物的事实来源。合成冲突样本证明，PDF 与 sidecar 内容不同时以 PDF 为准：既不会因旧 sidecar 的冲突误拦截，也不会因旧 sidecar 的“新鲜”内容掩盖实际 PDF 冲突。无法从 PDF 提取文字仍明确返回 `resume_text_unavailable`，不会被表述为 fresh。

## 实际对比

| 场景 | A/B 设置与相同限制 | 观测指标 | 结果 | 局限 |
| --- | --- | --- | --- | --- |
| 5-job 首次领取 | 同一临时 SQLite、同一 5 个已验证/合格岗位、同一绑定 manifest；A 从 `ad81f1c` 源码动态加载，B 为当前源码 | 实际材料事实谓词调用、领取 URL | A=1，B=1；均领取 synthetic job 0 | 只覆盖第一个可领取岗位；不代表浏览器/ATS 时间 |
| 10-job 首次领取 | 同上，10 个岗位 | 实际材料事实谓词调用、领取 URL | A=1，B=1；均领取 synthetic job 0 | 同上 |
| B 的只读快照 + 领取 | 同一 5/10-job cohort，`batch_progress(limit=10)` 后领取首项 | 快照事实读取次数、`next[0]` 与领取 URL、领取的 `admission_rows_scanned` | 5-job：5、相同、1；10-job：10、相同、1 | A 没有 `batch_progress`，因此没有可等价执行的旧快照；快照成本应只在需要展示/恢复状态时支付 |
| 已消费、unknown、确切 receipt、跨 batch 与连接重开 | 10-job 合成 manifest：一个精确 gate+receipt、一个 uncertain、一个 failed consumption、一个 active lease、一个 other-batch consumption；写连接和 `mode=ro` 重开连接分别投影 | `consumed`、状态分桶、两次投影是否相等、`next` 是否排除已消费/unknown | `consumed=3`；receipt=1、uncertain=1、consumed-without-receipt=1、in-progress=1、ready=6；重开前后完全相同，`next` 不含四类已占用项 | 是持久化合成 ledger，不是外部邮箱/ATS receipt |
| 跨 batch 容量 | 当前 manifest 外的同 batch consumption，`max_submissions=1` | remaining capacity / next | remaining=0、`next=[]`；旧/部分 manifest 不能回收已消耗的同 batch 名额 | 只验证数据库 ledger 的容量边界 |
| 过期 manifest / 材料变化 | 现有绑定契约；manifest 加载/领取检查有效期，`authorize_job` 检查实际上传 PDF 路径/字节绑定 | 是否被 projection/领取准入 | 不会进入 `next`，也不会领取；本次未改变该绑定机制 | 未模拟真实上传控件 |
| `.txt` sidecar 与实际 PDF 冲突 | 合成 source `.txt`、同名 PDF，二者 GPA 事实相反；PDF reader fixture 只返回 PDF 文本 | admission state/reason | 以 PDF 判定：PDF 冲突时 `stale_profile_fact`；PDF 与 profile 一致时不因 sidecar 误拦 | fixture 隔离了文本提取器，专门验证选择的附件而非 pypdf 解析质量 |
| malformed / unreadable PDF | 缺失或无法提取的上传 PDF | freshness state | `resume_unavailable` 或 `resume_text_unavailable`，均与 fresh 区分 | 后续实际是否允许进入人工质量审查仍由既有 decision/policy 契约决定；本次未新设缓存或放宽 gate |

## 验证

已运行：

```powershell
$env:PYTHONPATH = "$PWD\src"
$env:APPLYPILOT_DIR = (Join-Path $env:TEMP 'applypilot-batch-compare-20260905')
& C:\Users\raede\Desktop\简历\applypilot-local\.venv\Scripts\python.exe -m pytest tests\test_submission_admission.py tests\test_batch_progress.py tests\test_local_compat.py -k "actual_pdf or stale_profile_gpa or snapshot_next_matches or mixed_batch or partial_manifest or consumed_helper" -q
```

结果：`7 passed, 227 deselected in 3.92s`。

另运行一次临时内存 A/B harness：动态执行 `ad81f1c` 的领取函数与当前函数，使用同构 5/10-job SQLite cohorts。输出为：5-job `old_fact_reads=1,new_fact_reads=1`，10-job `old_fact_reads=1,new_fact_reads=1`，两组 `old_selected == new_selected == synthetic job 0`。

## 本次最小修复

- `evaluate_profile_resume_fact_freshness` 复用上传链路的 `resolve_resume_attachment`，保证 `.txt` 源路径也审查其实际 PDF。
- 去除 PDF 分支对同名 `.txt` sidecar 的优先读取；保留不可读 PDF 的显式 unknown 状态。
- 增加真实上传附件选择、PDF/sidecar 冲突，以及 5/10-job 快照/领取一致性和计数测试。

## 剩余阻断与不作的声明

- 没有真实 ATS、浏览器上传、邮箱 receipt 或真实简历的端到端测试；本报告不声称实际 ATS 吞吐、节省 token 或投递成功率。
- A 缺少持久化 `next` projection，故只能真实比较相同的领取路径；投影本身只能对 B 做功能和有界读取次数验证。
- 没有新增经验缓存或持久化结构；已有 durable material specialist 的重开 replay 行为维持原状，未把它当作本次性能优化证据。
