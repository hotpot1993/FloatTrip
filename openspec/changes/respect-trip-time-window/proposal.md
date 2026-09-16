## Why

用户在对话里说「11月7日傍晚抵达」，生成的行程却从 11月7日 上午 9:00 开始排。

偏差不是生成环节的某条规则写错了，而是系统里**根本不存在「抵达时刻」这个概念**：规划需求单的补丁协议只有 `destination / start_date / end_date / days / budget / 偏好 / 约束`（`app/chat/models.py:27-40`），规划图状态与逐景点结构都没有承载抵达时刻的字段（`app/planning/schemas.py:153-227`），而拼给 Planner 的 query 只用目的地、起止日期、天数、预算与约束文本拼接（`app/chat/service.py:300-312`）——**用户原话不会进入规划提示词**。

因此 9:00 不是代码里的默认值（后端 `09:00` 零硬编码），而是 Planner LLM 的行业惯例。换一种说法、换一次会话，问题就会以另一种形式重现。运行数据佐证：`data/langgraph-checkpoints.db` 中一个真实线程的 `query` 只有「云南，2026-07-25至2026-07-29」，而产出路线的 `day=1` 直接从 `period=morning, start_time=09:00` 开始。

同时，抵达时刻在现有设计下**不可能**被长期记住：记忆抽取会把任何含日期的内容判为一次性并主动拒绝（`app/chat/memory_service.py:34,50,609-622`）。这条约束是对的，本变更遵循它并把它写成决策记录（`docs/adr/0001-arrival-time-is-trip-scoped.md`）。

## What Changes

- 规划需求单新增与起止日期平级的 `arrival_time` / `departure_time` 一等结构化字段，随提交快照冻结。首次规划靠通用的快照透传即可；**修改行程不走这条路**——它由父行程的 planner checkpoint 逐字段重建，因此必须显式接入，否则「继续修改」会静默丢掉抵达时刻（见设计文档决策 1）。
- 抵达与返程时刻**永不写入长期记忆**，它们是行程属性而不是用户属性。
- 对话在用户**主动提到**抵达或返程信息但不够精确时发起单选追问（上午/中午/下午/傍晚/晚上/我填具体时间）；需求单新增两个可编辑时刻控件，可留空，且留空不阻止提交。
- 用户坚持模糊表述时保留原话，另外使用一张内部保守下界表作为提示词约束与核验阈值，不把下界当作"已知的真实抵达时刻"展示给用户。
- Planner 提示词新增首末两天的可用时段约束；`TIME_CHECK_SYSTEM` 新增两类违规（首日景点早于抵达可用下界、末日景点未在返程可离开时刻前留出缓冲），复用已有的 `time_check ⇄ planner` 重排回路。该回路只存在于首次规划图，修改流程没有 `time_check` 节点，因此核验不覆盖「继续修改」（既有行为，本次不扩大）。
- 首日只排晚餐与一个夜间可玩点，不排有白天开放时间限制的景点；末日景点必须在返程时刻前预留 2 小时缓冲结束。
- 边界退化：抵达晚于 21:00 时首日不排任何景点；单日行程不套用首末两条规则，只排抵达至返程之间的可用窗口。

## Capabilities

### New Capabilities

- `trip-time-window`: 抵达时刻与返程时刻作为行程范围内结构化事实的收集、确认、冻结、生成约束与核验，含模糊表述的保守下界与首末两天及边界场景的排法规则。

### Modified Capabilities

- `conversational-planning`: 其「Structured planning brief」需求扩展为包含抵达与返程时刻，并明确这两个字段是可选信息、缺失时不得阻止需求单进入可提交状态。

## Impact

- 后端：`app/chat/models.py`（`PlanningBriefPatch` 新增字段）、`app/planning/schemas.py`（`TravelPlanState` 新增字段）、`app/chat/executor.py`（`durable_fields` 白名单）、`app/planning/nodes.py`（首末日约束块与核验块）、`app/planning/prompts.py`（`PLANNER_SYSTEM`、`TIME_CHECK_SYSTEM`）、`app/chat/prompts.py`（追问规则）。
- 前端：`frontend/pages.jsx`（需求单可编辑摘要新增两个时刻控件、单选澄清控件接入追问）。
- 规格：新增 `trip-time-window`；修改 `conversational-planning`。
- 明确不受影响：`required_brief_fields()` 的必填集不变（`app/core/planning_brief.py:21-35`），因此需求单 readiness 规则与「可选项允许采用默认值」的既有约定都不变；长期记忆抽取规则不变；`structured-planning-constraints` 的有效约束快照机制不变。
- **已知残留**：只在用户主动提到时追问，因此用户完全不提抵达时间时首日仍按全天可排，结果仍可能是上午 9:00 起；中间天（首末之外的每一天）的起始时刻仍由 Planner LLM 决定，系统不定义。
- 存量行程与已有规划快照不回填这两个字段，旧行程保持原样可读。
- 需要同步 `CONTEXT.md` 的术语（已建立）与 `docs.md` 的能力描述。
