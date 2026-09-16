## 1. 结构化字段与链路贯穿

- [x] 1.1 在 `app/chat/models.py` 的 `PlanningBriefPatch` 新增 `arrival_time` 与 `departure_time`，并把它们加入 `strip_text` 校验器覆盖的字段列表
- [x] 1.2 在 `app/chat/executor.py` 的 `durable_fields` 白名单中加入这两个字段，确保它们进入需求单持久化数据
- [x] 1.3 在 `app/planning/schemas.py` 的 `TravelPlanState` 新增 `travel_arrival_time` / `travel_departure_time`，命名沿用既有的 `travel_start_date` 前缀
- [x] 1.4 在 `app/planning/helpers.py` 定义抵达可用下界与返程取值的对照常量，以及解析函数 `arrival_floor` / `departure_edge` / `departure_deadline` / `is_late_arrival`
- [x] 1.5 新增 pytest：快照中的 `arrival_time` / `departure_time` 能透传进 `TravelPlanState`（「对话补丁 → 需求单 → 提交快照」这两段需要 `langchain_core` 与 runtime，本环境跑不了，见文末环境限制）
- [x] 1.6 新增 pytest：缺少这两个字段的旧快照仍能正常转换为图状态
- [x] 1.7 新增 pytest：下界解析覆盖上午/中午/下午/傍晚/晚上、具体时刻、`N点` 写法与无法识别的表述；并覆盖抵达取「可开始活动」、返程取「最早离开」这一对相反的取值方向
- [x] 1.8 补齐两处**独立白名单**：`BriefPatch`（`app/api/runtime_routes.py`）与 `revision_snapshot_to_state`（`app/planning/runtime_worker.py`）——后者决定「继续修改」能否继承抵达时刻，漏掉只会在修改路径上静默失效
- [x] 1.9 新增 pytest：`revision_snapshot_to_state` 能从父行程的 planner checkpoint 继承这两个字段；旧行程的 checkpoint 没有该字段时取空；缺少 checkpoint 时仍然报错而不是静默降级

## 2. 收集与追问

- [x] 2.1 在 `app/chat/prompts.py` 补充规则：用户主动提到抵达或返程信息但不够精确时，先把原话写入字段，再发起一次针对该字段的澄清
- [x] 2.2 澄清使用既有 `DialogueClarification`，`field` 填对应字段名，`options` 固定为上午/中午/下午/傍晚/晚上/我填具体时间
- [x] 2.3 明确追问只做一次：用户回答后仍不精确就保留原话，不重复追问，也不影响 readiness；用户从未提及时不主动追问
- [x] 2.4 在 `frontend/pages.jsx` 的需求单可编辑摘要中新增抵达与返程时刻控件，可留空
- [x] 2.5 确认需求单保存走既有 `PATCH /api/planning-briefs/{id}`（`BriefPatch` 已补字段），服务端重新计算 readiness
- [x] 2.6 新增 JS 测试：摘要原样展示「傍晚」并标注未提供具体时刻；具体时刻不标注；只填一头时另一头显式显示未提供；两头都空时显示「未提供」
- [ ] 2.7 新增测试：用户提到「傍晚抵达」时触发澄清、回答具体时刻后字段被更新（需要对话图与 LLM，本环境无法运行）
- [x] 2.8 新增 pytest：抵达与返程时刻缺失时 `required_brief_fields` 仍返回空，需求单不受影响
- [x] 2.9 前端控件刻意用文本框而非 `input[type=time]`：time 控件遇到「傍晚」这类原话会显示为空，等于把用户说过的话弄丢；文本框用 placeholder 引导填 `18:30`

## 3. 抵达时刻不进入长期记忆

- [ ] 3.1 新增 pytest：包含抵达时刻的对话结束后，记忆抽取不产生与该时刻相关的长期事实（需要记忆抽取流程，本环境无法运行）
- [ ] 3.2 新增 pytest：抵达与返程时刻不出现在用户画像的任何分类中（同上）
- [x] 3.3 确认 `docs/adr/0001-arrival-time-is-trip-scoped.md` 中记录的理由与实现一致：字段是行程级结构化事实，未写入 `FACT_CATEGORIES`，也未进入 memory 抽取路径

## 4. 生成约束

- [x] 4.1 在 `app/planning/prompts.py` 的 `PLANNER_SYSTEM` 新增第 5 条规则：严格遵守抵达与返程时刻，并以 prompt 给出的具体限制为准
- [x] 4.2 在 `app/planning/nodes.py` 新增 `_time_window_block`，与 `_travel_dates_block` 一样注入 planner / reviewer / time_check 三处——只给 planner 会让 reviewer 不知情地把「首日只有一个点」当成节奏问题打回
- [x] 4.3 规则判定抽成纯函数 `helpers.time_window_plan`，由代码决定首末两天的处理，提示词只负责措辞，使这部分可单测
- [x] 4.4 实现深夜抵达退化：抵达可用下界晚于 21:00 时首日不排任何景点
- [x] 4.5 实现单日行程退化：`days == 1` 时不套用首末两条规则，只排可用窗口；窗口过窄或时刻无法识别时按最保守处理
- [x] 4.6 新增 pytest：傍晚抵达 → 首日只排晚间；深夜抵达 → 首日无景点；天数未知 → 不套用首末规则
- [x] 4.7 新增 pytest：单日行程在窗口够用时返回窗口，窗口过窄与时刻无法识别时分别走各自的保守分支
- [x] 4.8 新增 pytest：修改行程流程继承抵达与返程时刻
- [ ] 4.9 新增测试：给定抵达时刻为傍晚时，实际产出的首日不出现上午时段景点（需要真实 LLM 规划流水线）
- [ ] 4.10 新增测试：给定返程时刻时，末日景点在缓冲之前结束（同上）

## 5. 时间核验

- [x] 5.1 在 `app/planning/prompts.py` 的 `TIME_CHECK_SYSTEM` 由四类违规扩展为六类，新增首日早于抵达可用下界、末日晚于末日最晚结束时刻
- [x] 5.2 明确第 5、6 条只在 prompt 给出对应时刻时才成立，未给出时一律不成立，避免模型凭空造违规
- [x] 5.3 在 `app/planning/nodes.py` 的时间核查提示词中注入同一份抵达与离开约束（`_time_window_block`），使核验有可用事实
- [x] 5.4 记录覆盖面：`time_check` 只存在于首次规划图，修改流程图与兼容修改图都没有该节点，因此核验不覆盖「继续修改」——这是既有行为，已写入设计文档决策 9 与规格
- [ ] 5.5 新增测试：构造首日早于下界的行程，核验能识别并触发重排（需要真实 LLM 核验节点）

## 6. 文档同步

- [x] 6.1 在 `docs.md` 增加「问题十四」，记录新字段要穿过四处白名单、只在一半路径上失灵的根因与沉淀，并附跨层正则不一致的同类问题
- [x] 6.2 检查 `README.md`：其「设计亮点」为规划引擎技术亮点清单，未描述需求单字段，无相关描述需要同步，故不修改
- [x] 6.3 确认 `CONTEXT.md` 的「抵达时刻」「返程时刻」「抵达可用下界」三个术语与实现一致

## 7. 验证

- [x] 7.1 运行后端 pytest：`tests/test_time_window.py` 46 项全部通过；全量 84 passed / 34 errors，34 个 error 全部是环境缺少 `langgraph` 与 `langchain_core` 导致的既有失败，与本变更无关
- [x] 7.2 运行既有前端测试：`node --test tests/chat-state.test.js tests/navigation-state.test.js` 29 项通过；另用 Babel standalone 离线编译全部 6 个 `.jsx` 文件确认语法可编译
- [x] 7.3 规则层验证「用户完全不提抵达时间时行为与变更前一致」：`time_window_plan` 在两处都为空时返回 `None`，提示词块完全不注入
- [ ] 7.4 手工复现原始场景：在对话中说「11月7日傍晚抵达」，确认追问出现、首日不再从上午 9:00 开始（需要完整依赖与真实 LLM）
- [ ] 7.5 手工验证用户拒绝给具体时刻（回答「就傍晚吧」）时，首日按保守下界处理且需求单标注未提供具体时刻（需要完整依赖）
- [ ] 7.6 手工验证返程时刻生效，末日景点在缓冲前结束（需要完整依赖与真实 LLM）

## 环境限制

当前开发环境缺少 `langgraph`、`langchain_core`、`langchain_openai`、`redis`，`app.main` 无法导入，`app.chat.models` / `app.chat.executor` / `app.planning.nodes` 也无法导入。因此：

- **已用真实代码验证**：字段解析与规则判定（`helpers` 纯函数）、快照 → 图状态透传、修改流程从 checkpoint 继承、需求单 readiness 不受影响、前端摘要展示与判定一致。
- **仅完成实现、未能运行验证**（2.7、3.1、3.2、4.9、4.10、5.5、7.4–7.6）：涉及对话图、记忆抽取与真实 LLM 规划流水的行为。这些改动是字段接线与提示词条款，静态可读，但**没有任何自动化证据**证明模型真的遵守了新约束。

装齐依赖（`python -m pip install -r requirements.txt`）后，上述未完成项都可以执行。
