# Runtime Supervisor A/B comparison

日期：2026-09-05  
基线：A = `ad81f1c`，B = `a7cb610`  
结论：B 消除了正常启动、模型思考和长工具执行期间的 Supervisor 误干预，同时保留了真实重复无进展、effect 后未知和 CLI 无 steer 时的有界 fail-closed 行为。未发现需要在本工作树追加修复的复现缺陷。

## 方法与统一限制

- A 通过 `git show ad81f1c:src/applypilot/apply/application_supervisor_loop.py` 只读加载到独立 Python module；B 从本工作树 `src` 加载。两者使用相同事件、参数、虚拟时间和 `stall_window_seconds=2`。
- “干预次数”是 level 大于 0 的决策数；“误停”是合法等待场景中出现 `interrupt_park_manual` 的次数；toolcall 去重用同一 `tool_call_id` 的 proposed/started/completed 生命周期衡量。
- 未访问真实 profile、数据库、浏览器或账号；未发邮件或申请；未调用付费模型。结果是状态机、录制事件、fake transport 和 launcher fake-process 证据，不代表真实 ATS 吞吐或端到端投递成功率。

## 同输入 A/B 结果

| 场景 | A/B 设置与同输入 | 观测指标 | A：`ad81f1c` | B：`a7cb610` | 结论与局限 |
|---|---|---|---|---|---|
| 正常启动静默 | `start@0`；tick `2/4/6s` | 干预、误停、deadline 状态 | 3 次；`1→2→3`，6s 进入 `interrupt_park_manual`；误停 1 | 0 次；均为 `PROVIDER_STARTING_WITHIN_TURN_DEADLINE`；误停 0 | B 将合法启动静默交给外层总 deadline；未测真实 provider 启动延迟分布 |
| 模型等待 | `start@0`，`assistant.text@1s`；tick `2/4/6s` | 干预、误停、progress clock | 3 次；6s 手动 park；误停 1 | 0 次；`PROVIDER_THINKING_WITHIN_TURN_DEADLINE`；叙述不刷新真实 progress clock | B 区分 provider thinking 与 confirmed idle；未调用真实模型 |
| 长工具 | 同一 call `c1`：proposed `1s`、started `1.1s`、completed `90s`；tick `3/30/120s` | 干预、误停、toolcall 计数 | 5 次；同一生命周期被计为 3 次工具尝试，120s 手动 park；误停 1 | 0 次；同一生命周期只计 1 次，运行期为 `TOOL_RUNNING_WITHIN_TURN_DEADLINE`，完成后回到 thinking | B 修复生命周期重复计数和长工具误停；未声称真实 ATS 吞吐 |
| 真实重复无进展 | 4 个不同 call id、相同工具和参数，时间 `0/.1/.2/.3s`，无页面/控制/验证变化 | level、干预、最终动作 | `0→1→2→3`；3 次干预；第 4 次手动 park | `0→1→2→3`；3 次干预；第 4 次手动 park | B 没有放松真正的重复检测；事件是确定性录制输入 |
| effect 后结果未知 | 3 个不同 submit click，均带 `effect_started/submit_started/effect_uncertain` | replay 边界、receipt-only | 第 3 次为 level 4，`EFFECT_REPLAY_FORBIDDEN`，receipt-only | 同 A | B 保留最重要的不可重放边界；未做真实 receipt reconciliation |
| CLI 不支持 steer | 与真实重复场景相同；controller backend=`codex-cli`、`steer=None` | 干预、interrupt 次序 | 第 2 次审计；第 3 次因 `STEER_UNSUPPORTED` interrupt；共 2 次 controller 干预 | 第 2 次审计；第 3 次 `audit_only_steer_unsupported`；第 4 次 interrupt；共 3 次 controller 干预 | B 给无 steer CLI 多一个独立重复证据点，仍在第 4 次有界停止；不是无限等待 |

汇总：三个合法等待场景的 Supervisor 干预从 A 的 `3 + 3 + 5 = 11` 降为 B 的 `0`，场景级误停从 `3/3` 降为 `0/3`；两个安全关键异常场景（真实重复、effect 未知）最终处置不变。CLI 无 steer 的停止点由第 3 次重复延后到第 4 次重复。

## 总 deadline 所有权

B 的 provider lifecycle 状态只抑制基于静默的 Supervisor 升级，不取消 launcher 的总 wall-clock deadline。目标 launcher 测试证明：prepare 超时仍为 `failed:agent_runtime_timeout`，submit 超时仍为 `submission_uncertain`，且两者都不伪造 Supervisor 干预。因此结果是“避免提前误停”，不是“取消超时”。

## Model / effort 透传

| 路径 | 默认配置 | 显式 prepare 配置 | 实际消费证据 |
|---|---|---|---|
| Codex CLI authoritative runtime | 同一 model 参数；无映射时 effort=`high` | profile `prepare=medium` 胜过 environment `prepare=low` | resolved configuration 与 CLI command 共用；command 含 `--model <model>` 和 `model_reasoning_effort="medium"`；canary launcher 中 CLI 与 App Server 收到同一 resolved configuration |
| Codex App Server runtime | `RuntimeCellRequest.reasoning_effort` 默认 `high`；测试 wire 为 model=`gpt-5.6-sol`、effort=`high` | `prepare_repair=medium` | dispatch 前用 `model/list` 验证 model/effort；随后 thread/start 与 turn/start wire 携带 model，turn/start 携带 effort=`medium`；metadata 记录 source=`profile` 和 applied=`true` |

没有修改默认 effort，也没有更换 model。App Server 对无法由 `model/list` 证明支持的 model/effort 在 thread start 前拒绝，避免把不兼容配置越过 durable ambiguous dispatch 边界。

## 验证

```text
PYTHONPATH=<worktree>/src APPLYPILOT_DIR=<独有 temp> python -m pytest -q \
  tests/test_application_supervisor_loop.py tests/test_runtime_cell.py \
  tests/test_codex_app_server.py tests/test_app_server_runtime_wiring.py
99 passed in 4.96s
```

```text
PYTHONPATH=<worktree>/src APPLYPILOT_DIR=<独有 temp> python -m pytest -q \
  <CLI config/default nodes> <App Server config node> \
  <launcher silent-start/long-tool/total-deadline nodes>
9 passed in 2.57s
```

第二组的实际节点：

- `tests/test_apply_capabilities.py::test_runtime_configuration_is_shared_with_command_and_comparison_metadata`
- `tests/test_apply_capabilities.py::test_reasoning_resolution_records_default_fallback_and_rejects_unknown_values`
- `tests/test_runtime_cell.py::test_runtime_cell_request_carries_no_host_submission_authority`
- `tests/test_app_server_runtime_wiring.py::test_configured_turn_resolves_phase_effort_and_records_comparable_metadata`
- `tests/test_launcher_durable_runtime.py::test_run_job_canary_remains_observational_and_cli_owns_prepare_result`
- `tests/test_launcher_durable_runtime.py::test_run_job_silent_startup_watchdog_defers_to_total_deadline`
- `tests/test_launcher_durable_runtime.py::test_run_job_long_tool_lifecycle_is_not_silence_interrupted`
- `tests/test_launcher_durable_runtime.py::test_run_job_total_deadline_remains_authoritative_over_supervisor_silence`（prepare/submit 两个参数化 case）

## 剩余限制

- 对比不包含真实 Codex 服务、真实 ATS 页面、网络抖动或 5–10 岗位批次，因此不能外推吞吐、token 成本或申请成功率。
- fake transport 证明协议字段和前置验证，不证明特定线上 App Server 版本当前可用；线上 capability 仍须每次通过 `model/list`。
- 本轮未修改 launcher，也未新增抽象、fallback 或 retry；现有证据未支持进一步局部调优。
