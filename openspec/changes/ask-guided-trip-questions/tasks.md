## 1. 服务端提问模型

- [x] 1.1 在 `app/core/planning_brief.py` 定义固定六项提问清单（目的地、起止日期、抵达时刻、返程时刻、本次预算、旅行偏好），每项携带字段名或字段组、问法、候选项与服务端输入类型
- [x] 1.2 实现纯函数 `next_question(data)`，返回唯一的下一个待问字段或 `None`；必填字段在有值之前持续返回，可选字段一旦有值或在 `declined_fields` 中即跳过
- [x] 1.3 实现纯函数 `collection_complete(data)`，与 `required_brief_fields` 保持语义独立（`ready` 不属于本函数的输入）
- [x] 1.4 为 1.2 与 1.3 增加单元测试，覆盖：全空、只缺日期、只缺偏序中间项、全部跳过、全部回答、跳过后又自由输入另一个字段（`tests/test_brief_questions.py`）

## 2. brief 投影与白名单同步

- [x] 2.1 在 `PlanningBriefRepository._present` 增加 `declined_fields`、`question`、`collected`、`answered_fields`，均由 `data` 现算，不新增列、不写入 `missing_fields_json`
- [x] 2.2 同步 `declined_fields` 经过的字段白名单：`app/chat/executor.py` 的过滤集合已提升为 `app/core/planning_brief.py` 的 `DURABLE_BRIEF_FIELDS`，并加入 `app/chat/models.py` 的 `PlanningBriefPatch` 与 `app/api/runtime_routes.py` 的 `BriefPatch`。`TravelPlanState` 不需要它：该字段只属于需求单，不参与规划状态
- [x] 2.3 为「跳过抵达时刻之后语言模型写别的字段」写专门的回归测试 `test_skipped_field_survives_a_later_brief_patch`
- [x] 2.4 `planning_brief.*` 事件载荷带上新增投影字段；`PlanningBriefEvent` 白名单同步扩展（该模型是**图内**自定义事件的过滤器，chat 侧载荷由 `apply_brief_patch` 直接发布，不走它）

## 3. 结构化回答接口

- [x] 3.1 新增端点 `POST /api/planning-briefs/{brief_id}/answers`，请求体 `{field, value}`，返回更新后的 brief（内含 `question` 与 `collected`）
- [x] 3.2 校验 `field` 等于当前待问字段或字段组，不匹配返回 409 且不修改 brief
- [x] 3.3 按字段校验取值：日期范围两个日期均可解析且不倒序、抵达/返程时刻长度受限、预算长度受限、偏好只能取服务端给出的候选项
- [x] 3.4 回答「还不确定」时写入 `declined_fields` 且不写字段值；必填字段拒绝跳过
- [x] 3.5 复用 `chat_service.update_brief` 的校验与记忆刷新路径，确保结构化回答与自由输入写出的 brief 结构完全一致
- [x] 3.6 接口测试：正常推进、连续回答、字段不匹配 409、坏日期 422、必填不能跳过 422、越权 404、已提交后 409、跳过后不再被问

## 4. 前端活动投影与提问卡片

- [x] 4.1 把 `ChatState.activityItems` 改为允许同一条 brief 派生多个条目：已处理字段的折叠记录行（`brief-record:{id}:{field}`）、当前提问（`brief-question:{id}:{field}`）、需求摘要（`brief:{id}`）
- [x] 4.2 派生条目的 key 稳定，事件重放或刷新时不重复追加；需求摘要只在 `collected` 为真时出现
- [x] 4.3 实现提问卡片组件：字段化问法、候选项、日期范围、自由输入框、剩余项数、逐题跳过；不提供一次性跳过全部
- [x] 4.4 候选项与日期范围调用结构化回答接口并就地更新；自由输入走既有消息通道；两者都不产生助手气泡复述问题
- [x] 4.5 新提问出现时滚动到卡片并聚焦；不使用额外的 live region 播报，避免与焦点移动重复
- [x] 4.6 澄清提问复用同一种卡片呈现：候选项随当轮事件下发、不落库，刷新后自动退化为已持久化的文本气泡
- [x] 4.7 需求摘要一旦出现即就地更新、永不收回（`collected` 只增不减；必填被清空时摘要仍在，缺字段处显示待补充）
- [x] 4.8 前端 reducer 测试：派生条目顺序、折叠态、剩余项数、重放去重、刷新后一致、澄清选项附着（`tests/chat-state.test.js`）

## 5. 确认契约与提示词

- [x] 5.1 改写 `_confirm_plan`：用户要求开始时结束剩余提问、展示需求摘要，不创建 PlanningRun；移除直接提交分支
- [x] 5.2 增加 `_end_remaining_questions`，把剩余未处理可选项一次性标记为已处理；无变化时不写库
- [x] 5.3 改写 `DIALOGUE_SYSTEM`：提问由系统负责、模型不得复述问题或罗列缺失字段；规划意图但无可提取字段也用 `create_plan`；时段词即终局答案，不再追问钟点；新增 `declined_fields` 写入规则
- [x] 5.4 在 `application_state` 的 `planning_brief` 中提供 `question`、`collected`、`declined_fields`，使自由输入的回答能被正确归位
- [x] 5.5 更新提示词契约测试：`test_prompt_hands_questioning_over_to_the_server`、`test_brief_patch_carries_declined_fields`；`tests/data/dialogue_eval_cases.json` 无需改动（其中没有抵达或澄清类用例）

## 6. 顺带修复

- [x] 6.1 把 `app/core/planning_constraints.py` 的旧偏好字段改成一次性迁移（投影后移除键），消除「改一条派生约束变成两条」；增加 5 项回归测试
- [x] 6.2 为「清除这份需求」接入二次确认与结果说明，复用现有 `ConfirmModal` 组件

## 7. 验证

- [x] 7.1 后端全量 pytest：**149 passed / 0 failed / 37 errors**。37 个错误全部是 `langgraph`（31）与 `langchain_core`（6）缺失导致的收集失败，与改动前基线同类
- [x] 7.2 `node --test tests/chat-state.test.js tests/navigation-state.test.js`：**42 passed / 0 failed**
- [x] 7.3 离线 JSX 语法校验器：6 个 `.jsx` 全部通过；CSS 括号配平 980/980
- [ ] 7.4 手工验证完整流程（见下方环境限制）
- [ ] 7.5 核对三种回潮现象（见下方环境限制）

## 环境限制

本机 Python 环境（`C:\Users\HotPot\AppData\Local\Python\pythoncore-3.14-64\python.exe`）缺少 `langgraph`、`langchain_core`、`langchain_openai`、`redis`。`app/api/runtime_routes.py` 与 `app/chat/executor.py` 的导入链会经过前两个包，因此下列内容**已实现、已写测试，但未在本机执行**，必须在补齐 `requirements.txt` 的环境中跑一遍：

- `tests/test_runtime_api.py` 的 3 个新用例：结构化回答推进、已提交后拒绝回答、投影暴露当前提问
- `tests/test_dialogue_actions.py` 的 6 个新用例：空字段开单、确认不建 Run、必填继续问、幂等重复确认、跳过跨写存活、模型不能跳过必填
- `tests/test_chat_agent.py` 的 2 个新用例：提示词交还提问权、`declined_fields` 进 schema
- `tests/test_frontend_delivery.py` 的资源版本断言（已同步为 `20260916-guided-trip-questions`）

人工验证（7.4 / 7.5）同样依赖真实语言模型，需在该环境中走一遍：一条消息给全信息直接出摘要；只说「我想出去玩」进入逐项提问；候选项点击推进且不产生助手气泡；跳过后不再被问；刷新后状态一致；「别问了直接开始规划」出摘要但不建 Run；确认页面上不出现「被问过的字段又被问一遍」与「助手气泡复述问题」两种回潮。
