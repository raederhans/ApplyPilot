# Workday / SmartRecruiters semantic prepare fast path comparison

日期：2026-09-05  
工作树基线：`a7cb610`  
旧版对照：`ad81f1c`  
范围：仅本地 fixture、无登录临时浏览器、无真实 ATS 请求、无模型调用、无申请/邮件/Submit。

## 结论

新 fast path 在 Workday 与 SmartRecruiters 的 routine email 文本框和原生 Country `<select>` 上，已通过真实 Edge/Chromium DOM、生产 `PlaywrightProductionSemanticBatchAdapter` 与生产 durable semantic batch runtime。`canary` 成功时产生一次经 postcondition 证明的 host write，并返回 `ready_to_submit`，launcher 的既有回归证明此分支不启动 prepare Agent；`off` 与 `shadow` 都继续 Agent，`shadow` 保持零写。

首轮真实浏览器测试发现一个局部覆盖缺陷：生产 inspector 把 Country 归一为 `location`，而 batch adapter 只按计划 semantic `country` 精确匹配，因此两家 provider 的原生 select 都安全 fallback。已在 adapter 内加入窄、来源 semantic 受限、唯一匹配仍 fail-closed 的 label alias；原生 select 先机械剥离 inspector 附加在 wrapping label 尾部的已知 option 文本，再要求剩余标签精确匹配。相同用例由 2 个失败变为 2 个 `verified`，而 `Country of birth`、`Country of citizenship`、`State of tax residence` 均明确拒绝。

postcondition 契约保留：adapter 写后执行两次一致性观察；fixture 在 `change` 后回滚值时，runtime 返回 `parked`、`effect_count=1`、`legacy_fallback_safe=false`。二次 audit 契约也保留：outer worker 对任何 `ready_to_submit` 在 submission gate 前重新调用 live pre-submit audit；浏览器用例在 advanced page epoch 上再次 inspection 并确认写入值。

## 实际对比

所有浏览器行使用同一份 HTML、同一字段值、同一 provider host 路由、同一 no-submit/no-model 限制。观测指标只使用控制流、DOM、durable runtime 状态与副作用计数；未用墙钟时间声称真实 ATS 吞吐。

| 场景 | A / B 设置 | 同输入与同限制 | 观测指标 | 结果 | 局限 |
|---|---|---|---|---|---|
| 旧版与新 fast path | A=`ad81f1c`；B=`a7cb610` canary | email=`fixture@example.test`；Workday/SmartRecruiters；无模型/Submit | old source 是否存在 pre-Agent hook；B `status/disposition/effect_count/agent_invoked` | A 无 `run_prepare_fast_path` hook，routine repair 必须进入 Agent；B=`verified/ready_to_submit/1/false` | A 的 Agent 未实际启动，因为任务禁止模型调用；这是旧源码控制流与新 launcher sentinel 测试的对照，不是延迟基准 |
| feature modes | off / shadow / canary | 两家 provider、相同空 email 与 fixture | audit 次数、batch 次数、DOM value、Submit event | off=`continue_agent`, audit 0, batch 0, effect 0；shadow=`shadow_match/continue_agent`, audit 1, effect 0；canary=`verified/ready_to_submit`, audit 1, effect 1；三者 Submit event 均 0 | fixture 不包含真实站点 hydration、登录或反自动化 |
| 原生 Country select（修复前后） | A=精确 semantic；B=剥离已知 option 尾部后的精确 label alias | 两家 provider、Country=`Singapore`、相同 `<select>` options | runtime status、effect_count、最终 DOM value、敏感近似标签拒绝 | A 两家均 `fallback`, effect 0；B 两家均 `verified`, effect 1, DOM value=`sg`；birth/citizenship/tax residence 3 类均拒绝 | 只证明代表性原生 select；不证明所有地区文案 |
| 动态选项漂移 | 初始有 Singapore / dispatch 前删除 Singapore | Workday；相同 country patch；无其他 DOM 变化 | page signature、status、effect_count、fallback safety | `fallback`, effect 0, `legacy_fallback_safe=true`，DOM 仍空 | 未覆盖异步网络加载的全部竞态，只覆盖 dispatch 前结构漂移 |
| 非 routine 控件 | checkbox / file upload / custom combobox | 相同 fast-path audit/plan 入口，真实 DOM 同时包含 legal、resume、department | 是否调用 batch、DOM 状态、Submit event | 三类均在 pre-batch fallback；batch calls=0；checkbox 未勾选、file 为空、custom 无值、Submit=0 | 不测试旧 Agent 如何处理这些控件；仅证明 fast path 不接管 |
| 写后未知 | email `change` 后 microtask 回滚 | SmartRecruiters；相同 email patch | first effect、second observation、runtime status、fallback safety | `parked`, effect 1, `legacy_fallback_safe=false`；不会降级到 Agent 重写 | fixture 人工制造 drift，但穿过真实 DOM writer 与生产 runtime |
| 二次 audit | canary verified 后 re-inspect advanced epoch | 两家 provider；相同 browser/page lease | advanced page epoch、descriptor/value、outer-worker code path | re-inspection 唯一找到 email，值仍为预期；outer worker 在 gate 前仍强制 live audit | 未执行最终 submission gate 或 Submit |

每个浏览器 context 对全部请求注册 `page.route("**/*")`：仅精确 fixture host 被本地 fulfill，其他 URL 立即 abort；本轮断言 `unexpected_requests=[]`。独立 Edge 使用临时 profile 与 CDP `9556`，测试后已停止并确认端口释放。

## 局部修复

- `src/applypilot/apply/semantic_batch_adapter.py`
  - 仅允许 `country` 从 inspector 的 `location` semantic，经剥离已知 native option 尾部后的严格完整 label pattern 映射到计划 semantic；未从本 fixture 复现的其他 semantic 不增加 alias。
  - alias 同时要求来源 semantic 正确；例如 `ordinary_text` 的 `Country code for phone` 不会被当作 country。
  - `control_for`、写前 descriptor recheck 与 `pristine` 复核共用同一匹配规则；仍要求唯一控件，缺失或歧义继续 fail-closed。
- `tests/fixtures/apply/semantic_batch_browser.html`
  - 代表性 email、native select、legal checkbox、resume upload、custom combobox、Submit，以及动态 option/postcondition drift 钩子。
- `tests/test_semantic_batch_adapter_chromium.py`
  - 默认启动独立无登录 Playwright Chromium（bundled runtime 不可用时才用 Edge channel）；本轮可用 `APPLYPILOT_TEST_CDP_ENDPOINT=http://127.0.0.1:9556` 连接明确 owner 的临时 Edge。
  - 两家 provider 的全部目标请求均本地拦截，无真实 ATS 网络访问。

未修改 `launcher.py` 或 `worker_orchestration.py`。如 root 需要将本轮经验加到相邻所有权文件，建议只保留如下契约，不要复制 adapter label 规则：

```python
if fast_path.disposition == "ready_to_submit":
    # Do not grant Submit here. The outer worker must perform its normal
    # live pre-submit audit and SubmissionGate before any final action.
    return "ready_to_submit", duration_ms
```

## 验证记录

使用固定 Python：`C:/Users/raede/Desktop/简历/applypilot-local/.venv/Scripts/python.exe`，并将 `PYTHONPATH` 绑定到本 worktree 的 `src`；`APPLYPILOT_DIR` 使用本任务独有 `.tmp`。

1. 首轮 CDP 浏览器复现：`13` tests 中 `3 failed, 10 passed`；两个产品失败均为 Country semantic mismatch，另一个是 fixture 的 `currentTarget` 生命周期问题。
2. 修复后 CDP 浏览器用例：`13 passed in 6.27s`。
3. CDP 窄合同回归（fast path、launcher sentinel、adapter、runtime、浏览器）：`65 passed in 8.28s`；此轮早于 3 个敏感 alias 拒绝用例。
4. Ruff：`All checks passed!`。
5. 不设置外部 CDP、模拟 CI 自启独立 Chromium：浏览器文件 `13 passed in 18.53s`；加入敏感 alias 拒绝用例后的最终窄合同回归 `68 passed in 20.15s`。

## 未验证与剩余风险

- 本地浏览器 fixture **不能证明真实 ATS 覆盖率**。未验证真实 Workday/SmartRecruiters 的租户差异、前端版本、React 状态同步、跨源 iframe、Shadow DOM、登录、WAF/CAPTCHA、远端 CI 或真实投递。
- 未启动旧版或新版 prepare Agent，因此没有模型成本/延迟实测；能确认的是 canary verified 分支不调用 mocked `Popen`，不能换算成真实吞吐提升。
- 未测试 5–10 个真实岗位，也未做 Submit。此报告只支持“代表性 routine control 的 host fast path 契约成立”，不支持发布或真实 ATS 兼容率声明。
- alias 只覆盖窄英文标签；其他语言或供应商文案会继续安全 fallback。扩大词表应由新的真实失败证据驱动。
