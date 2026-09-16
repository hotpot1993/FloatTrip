## Why

`redesign-conversation-interaction-flow` 于 2026-09-16 归档。归档前逐条核对了它的四个能力规格与代码事实，发现 `tasks.md` 有勾选失真：有三处行为被标为已完成，代码里却不存在。为了让主规格库只描述真实存在的行为，这三条没有随归档进入 `openspec/specs/`，而是留档在这里等待补齐。

本 change 目前只记录差距，不包含 spec delta。原因：其中两条的目标行为尚未设计（一条需要后端新增 action descriptor，一条需要重写 Run 卡的进度区），直接照抄旧条文会把未经再确认的设计写进规格库。补齐实现时再补 delta。

## What Changes

补齐下列已承诺但未实现的行为：

1. **运行中新增约束的显式选择。** 用户在正式规划运行期间追加约束（例如“不要安排丽江”）时，页面应提供「停止并按新要求重新规划」或「完成后创建修改任务」的明确选择，而不是只在对话里回一句文案。现状：`frontend/style.css:1952-1954` 存在 `.change-decision` 样式，但没有任何组件引用它；后端也没有可执行的决策事件或 action descriptor。
2. **Run 卡的产品阶段进度。** 页面应把内部节点映射为少量稳定阶段并默认隐藏内部节点名。现状：`frontend/chat-state.js:234-259` 已经维护 `PRODUCT_STAGES` 与单调推进逻辑，但 JSX 中零引用；`frontend/pages.jsx:1132-1149` 仍在渲染 `frontend/components.jsx:7-15` 的 7 站内部循环标签（如“规划 ⇄ 评审行程”），并把后端节点标签注入当前站小字。
3. **关键状态播报包含任务名称。** `waiting_user` 转变的 live region 播报应包含任务名称与所需动作。现状：`frontend/pages.jsx:607-613` 的文案是硬编码的「有一项旅行规划需要你的回复」，不含任务名。

## 同时记录的次要差距

以下问题在同一轮核对中发现，未达到需要改写规格的程度，一并记录以免丢失：

- **认证后恢复目标未覆盖全部入口。** `frontend/main.jsx:206` 与 `:210` 的「历史行程」「我的画像」只传 reason、不传 continuation，登录后不会回到目标页；`conversation-entry-flow` 的 `认证后恢复目标` 只对旅行对话与行程详情成立。
- **前端缺少 DOM 组件测试设施。** `tests/` 下没有 jsdom / react-dom / Playwright，`tests/chat-state.test.js` 与 `tests/navigation-state.test.js` 只覆盖纯逻辑；`tests/test_frontend_delivery.py` 只断言字符串存在。空状态引导、缺字段追问的实际控件与焦点、确认卡防重复点击、离屏摘要栏、键盘顺序、窄屏抽屉均无自动化覆盖。
- **失败卡不读 `error_public.retryable`。** `app/runtime/models.py:56-59` 已经给出该字段，`app/runtime/scheduler.py:115-124` 会产生 `retryable=False` 的失败，但 `frontend/pages.jsx` 对所有 `failed` 一律显示重试按钮。
- **死代码与陈旧文档。** `frontend/pages.jsx:2468` 的 `HomePage` 已无引用；`docs/conversation-entry-migration.md:5` 仍写着“顶部『新建规划』进入旅行对话”，与现状不符；`frontend/chat-state.js:194` 的 `dates_or_days` 标签后端从不产出。

## Impact

- 主要影响 `frontend/pages.jsx`、`frontend/chat-state.js`、`frontend/components.jsx`、`frontend/style.css`；第 1 项还需要 `app/chat/` 或 `app/runtime/` 提供可执行的决策事件。
- 与前一个 change 不同，本次补齐不改变 Run 生命周期、事件白名单或数据模型；第 1 项若走结构化 action descriptor，需要扩展 `app/runtime/models.py` 的自定义事件白名单。
- 第 3 项同时需要在首次引入 DOM 组件测试设施时决定测试栈，这项决定会长期影响前端测试形态，因此不并入任何单一功能改动。
