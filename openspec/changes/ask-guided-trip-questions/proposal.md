## Why

当前用户只要说一句「帮我规划去云南旅行」，后端就立刻创建一条 `collecting` 的 PlanningBrief 并广播 `planning_brief.updated`，前端随即把 **TRIP NOTE 卡片**（`frontend/pages.jsx:770-990`）插进时间线——此时目的地和日期都还是空的。用户看到的是一张写满「还没决定」「日期待补充」的摘要，而不是被引导着把需求说清楚。

同时，「该问什么」这件事根本没有人负责：后端只计算必填字段全集（`app/core/planning_brief.py:21-35`），追问文本完全由语言模型临时决定；`DialogueClarification.field` 与 `.options`（`app/chat/models.py:67-70`）从定义出来就没有任何消费方，`executor.py:31` 只取走 `.question` 当一句纯文本，选项从未到达前端。

这留下一个已知缺口：用户不主动提抵达时刻时，第一天仍会从 09:00 开始排行程——正是 `respect-trip-time-window` 想解决却明确搁置的那一半。

本次改动把交互顺序倒过来：**先提问、后出摘要**。用户通过逐项提问把要求说清楚，六项都有交代之后，才形成完善的 Planning Brief 并展示 TRIP NOTE 交由确认。

## What Changes

- **BREAKING**：正式规划的确认契约改为「对话中的开始请求一律先出摘要」。用户说「别问了，直接开始规划」时，系统结束剩余提问并展示需求摘要，必须由用户对摘要执行确认动作才创建 PlanningRun；移除仅凭一条对话消息直接创建 Run 的例外。
- 新增服务端确定性提问：由服务端依据 PlanningBrief 当前内容与已跳过字段算出唯一的下一个待问字段，并下发结构化问题（字段、问法、候选项、剩余项数）。语言模型不再决定问什么。
- 提问清单固定为六项并严格串行：目的地 → 起止日期 → 抵达时刻 → 返程时刻 → 本次预算 → 旅行偏好。任何时刻时间线上最多一张待答卡片。
- 新增结构化回答接口：候选项点击直接写字段，不经过语言模型、不产生对话消息；自由输入仍走既有对话通道。
- 提问作为活动时间线条目追加，答完就地折叠为一行「已记录」；不再由「一个实体 → 一个时间线项」的投影直接渲染需求摘要。
- TRIP NOTE 改由「收集已完成」这一展示层派生量触发，并且一旦出现就不再收回；`status`、`ready` 与 `submit()` 的语义一字不改。
- 「已问过/已跳过」全部由 PlanningBrief 派生，跳过记录写入 `data.declined_fields`，不新增表、不新增列、不需要数据迁移。
- 提问期间新增一条主规格要求：`ready` 只表示必填信息完整且可提交，不表示提问已经结束。
- 顺手修两个相邻旧缺陷：`planning_constraints.py` 中旧偏好字段每次读取都被重新派生，导致同一偏好出现两条；`discardPlanningBrief` 没有二次确认，点一下卡片就静默消失。

## Capabilities

### New Capabilities

无。本次改动是既有引导式收集能力的实现方式变化，不是新能力；再开一个近义能力会让主规格库出现两个难以分辨的对象。

### Modified Capabilities

- `guided-planning-brief`：提问来源由语言模型改为服务端固定清单、问题使用匹配控件、可选字段允许跳过、确认摘要的出现时机与不收回、提问卡片的生命周期与无障碍定位、澄清与字段提问的呈现一致性。
- `conversational-planning`：规划意图在没有任何可提取字段时也创建 brief 并开始提问；明确 `ready` 与「收集已完成」是两件事；正式规划的确认不再存在直接开始例外。

## Impact

- 后端：`app/core/planning_brief.py`（新增提问清单与下一个待问字段的纯函数）、`app/chat/executor.py`（不再由 clarification 决定追问、`durable_fields` 增加跳过记录、`confirm_plan` 改为结束提问而非提交）、`app/chat/prompts.py`（不再由模型提问、不再自行复述问题）、`app/chat/models.py`、`app/chat/service.py`（application_state 增加当前提问）、`app/runtime/repositories.py` 与 `app/api/runtime_routes.py`（brief 投影增加提问字段、新增结构化回答端点）。
- 前端：`frontend/chat-state.js`（活动投影改为一条 brief 派生多个条目、提问派生、跳过与进度投影）、`frontend/pages.jsx`（提问卡片组件、焦点与滚动、确认弹窗接入清除需求）、`frontend/style.css`、`frontend/api.js`。
- 数据：无表结构变更、无数据迁移。`brief["data"]` 新增 `declined_fields` 键，需要同步它经过的四处字段白名单（这是本仓库已记录过的老毛病，见 `docs.md` 问题十四）。
- 与移动端无关：`mobile-app/` 与 `mobile-prototype/` 不在本次范围内。
- 与已归档的 `redesign-conversation-interaction-flow` 的关系：该 change 的 `guided-planning-brief` 描述的是「由语言模型渐进追问」，本次把提问权移到服务端，因此对它做 MODIFIED 而不是另立能力。
