## Why

历史行程页目前是只读的：卡片上唯一的操作是整卡点击打开（`frontend/pages.jsx:1965-1967`），后端也只有两个 GET（`app/api/history_routes.py:23,31`），全仓库没有任何删除行程的代码路径。用户产生的行程只能不断累积，而列表固定返回最近 50 条（`app/core/memory.py:241`），既无法清理也不分页。

更深的问题是「行程归档」这个能力**至今没有任何规格覆盖**：`conversation-entry-flow` 只声明"历史行程入口继续保留"，列表与详情都是无规格的裸功能。本次在补上删除的同时，把这个能力正式建成规格。

## What Changes

- 新增删除行程的能力。删除的作用单位是**一条行程**（`itineraries` 表的一行 = 一版可打开的路线内容），不是"一趟旅行"；同一趟旅行派生出的其他版本、来源对话与规划任务记录全部保留。
- 新增 `DELETE /api/history/{plan_id}`，沿用同文件既有的鉴权与错误语义（未登录 401 / 不存在 404「行程不存在」/ 非本人 403「无权访问」）。
- **BREAKING（数据完整性）**：`runs.result_itinerary_id` 是指向行程的唯一真外键且未声明 `ON DELETE`（`app/core/database.py:122`），而每次连接都启用 `PRAGMA foreign_keys=ON`（`app/core/database.py:335`），因此只要有任何 Run 引用该行程，删除就会抛 `IntegrityError`。删除必须在同一事务内先把这些 Run 的行程引用置空。
- 删除**不可恢复**，需要显式确认：历史行程卡片右上角新增常驻删除图标，点开一个新建的通用确认弹窗，展示目的地、日期与不可恢复警告。
- 把「行程归档」建成正式 capability，覆盖列表、详情与删除。

## Capabilities

### New Capabilities

- `itinerary-archive`: 行程归档的对外契约——列表、单条行程详情、所有权隔离、删除的连带边界与不可恢复语义。

### Modified Capabilities

无。现有主规格均未覆盖行程归档，因此本次只新增能力，不修改既有条文。

## Impact

- 后端：`app/api/history_routes.py`（新增 DELETE 路由）、`app/core/memory.py`（新增删除与引用置空）、`app/runtime/repositories.py`（Run 的行程引用置空与查询）。
- 前端：`frontend/api.js`（新增删除封装）、`frontend/pages.jsx`（`HistoryPage` 卡片与确认弹窗）、`frontend/style.css`。
- 数据影响：`itineraries` 行被删除；`runs.result_itinerary_id` 被置空；`messages.related_itinerary_id` 与 `itineraries.parent_id` 允许悬空。
- 明确不受影响：对话与消息不被级联删除（`conversations` / `messages` 与 `itineraries` 之间没有外键）；规划任务记录、执行事件与重试链路保留；长期记忆与画像事实不受影响。
- 画像页的「趟旅程」计数会随删除下降，因为它直接 `SELECT COUNT(*) FROM itineraries`（`app/api/profile_routes.py:58-61`）——这是正确语义，不需要额外处理。
- 仅覆盖 Web 端 `frontend/`；`mobile-app` 与 `mobile-prototype` 不在本次范围。
- 需要同步更新 `README.md` 与 `docs.md` 中与历史行程相关的功能描述。
