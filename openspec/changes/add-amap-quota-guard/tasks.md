# 任务：高德配额保护

> **状态说明（实现完成后填写）**：第 1–8 节已完成；第 9 节分为「本机可执行」与
> 「依赖完整环境」两类，逐条标注；第 10 节已完成；第 11 节是交付给用户的实测与
> 对账待办，**无法由代码完成**。实现过程中有两处设计被测试推翻并重写，见文末。

## 1. 领域层：池模型与限额

- [x] 1.1 新建 `app/core/amap_quota.py`：定义 `QuotaBucket` 枚举（`search` / `weather` / `lbs`）与池到官方服务组名的映射（注释里写明每个池含哪些接口）。*先做：后面所有模块都依赖它。*
- [x] 1.2 定义三个池的默认限额常量（搜索 4,750 / 天气 4,750 / LBS 0）与预警阈值比例（0.8），注释写明官方原值 5,000 / 5,000 / 150,000 及来源链接。*依赖 1.1。*
- [x] 1.3 实现限额解析与环境变量读取（`AMAP_QUOTA_SEARCH_LIMIT` / `AMAP_QUOTA_WEATHER_LIMIT` / `AMAP_QUOTA_LBS_LIMIT` / `AMAP_QUOTA_WARNING_RATIO`），校验必须为 `>= 0` 的整数，非法值启动即失败而不是静默当作不限。*依赖 1.2。*（校验在 `app/main.py` 启动时调用。）
- [x] 1.4 实现 `Asia/Shanghai` 月份边界计算（返回 `YYYY-MM`）。**写死时区，不读容器 `TZ`**，并在注释里标注"官方未公布重置时刻，此处按自然月推断"。*依赖 1.1。*
- [x] 1.5 定义 `AmapQuotaExceeded` 异常，携带 `public_code`（`amap_quota_exhausted`）与含池名、重置时刻的 `public_message`；文案里的时刻必须复用 1.4 的计算，不得硬编码字符串。*依赖 1.4。*

## 2. 持久化

- [x] 2.1 `app/core/database.py`：新增 `amap_quota_usage` 表（`bucket` + `month` 复合主键，`counter_calls` / `counter_baseline` / `counter_at_reconcile` / `warning_sent_at` / `updated_at`），沿用 `CREATE TABLE IF NOT EXISTS` 风格。
- [x] 2.2 新建 `app/core/amap_quota_store.py`：原子预留（`BEGIN IMMEDIATE` 事务内检查余量并自增，不足时返回失败而不写入）与读取余量。*依赖 2.1。*（**未**放进 `app/runtime/repositories.py`：配额是跨层基础设施，仓储模块只放 Run/Brief/Conversation；放在 `core/` 还让这一层完全不依赖 LLM 栈，可本机测试。）
- [x] 2.3 实现用量对账：写 `counter_baseline` 与 `counter_at_reconcile`，**不覆写 `counter_calls`**。*依赖 2.2。*（语义在实现中被测试推翻并重写，见文末。）
- [x] 2.4 实现预警状态读写（`warning_sent_at`），保证同池同月只发一次。*依赖 2.2。*
- [x] 2.5 旧行清理：保留 13 个月，`init_db` 调用 `delete_old_months`。*依赖 2.1。*（刻意复用 `amap_quota_store` 的实现，而不是在 `init_db` 里写 SQL 日期运算——领域层月份边界固定在 `Asia/Shanghai`，SQL 的 `strftime('now')` 用 UTC，两份时间基准会让「哪些月份算过期」互相矛盾。）

## 3. 调用通道：错误分类

- [x] 3.1 `app/providers/amap/client.py`：把 `direction/walking` 的 URL 从 `app/api/plan_routes.py:595` 收进常量。
- [x] 3.2 在 `client.py` 定义 `QUOTA_INFOS` / `ACCESS_DENIED_INFOS` / `THROTTLE_INFOS` / `SPECIAL_INFOS`，并同时收录**数字 infocode 与名字 info 两种表示**。注释写明日配额码虽已取消但仍须归入 `quota` 的理由，以及月配额返回码未文档化这一事实。*依赖 3.1。*
- [x] 3.3 实现 `classify_failure`：**同时读 `infocode` 与 `info`**，返回 `quota` / `access_denied` / `throttle` / `special` / `other`。*依赖 3.2。*

## 4. 调用通道：唯一出口

- [x] 4.1 新建 `app/providers/amap/channel.py`，提供单一 async 入口 `call_amap`，参数为 URL、查询参数、池归属，返回已解析 JSON。*依赖 1.5、2.2、3.3。*
- [x] 4.2 固定内部顺序：失败分类 → 预占额度 → `provider_slot("amap")` → 发请求。**占额早于任何网络动作**。*依赖 4.1。*（`app/core/http.py` 的 `http_get_json_async` 新增 `max_attempts` 参数，通道传 1 以接管重试——否则重试埋在它的循环里，计数点看不见。）
- [x] 4.3 额度不足时抛出 `AmapQuotaExceeded`，**不发出网络请求**。*依赖 4.2。*
- [x] 4.4 重试路径：命中 `throttle` 时退避重试，且**每次重试同样预占额度**；命中 `quota` 时立即抛出、不重试。*依赖 4.2。*
- [x] 4.5 `IP_QUERY_OVER_LIMIT`（含数字码 `10010`）单独处理：`public_code` 为 `amap_ip_restricted`，文案说明需要提工单、请勿等待自动恢复。*依赖 4.2。*
- [x] 4.6 `INVALID_USER_KEY` / `SERVICE_NOT_AVAILABLE` / `USER_KEY_RECYCLED`（含数字码 `10001` / `10002` / `10009`）归入权限类，`public_code` 为 `amap_access_denied`。*依赖 4.2。*

## 5. 让既有调用方走通道

- [x] 5.1 `app/providers/amap/poi.py`：`search_around_pois_async` / `search_attraction_pois_async` / `search_city_pois_async` 改走通道。*依赖 4.1。*
- [x] 5.2 `app/providers/weather/amap.py`：`fetch_forecast_async` 改走通道（`weather` 池）；**配额耗尽不再降级**，让它冒泡到 Run。*依赖 4.1。*
- [x] 5.3 `app/api/plan_routes.py`：`/api/poi/search`、`/api/poi/nearby`、`/api/route/walking` 三条路由由 `def` 改 `async def` 并改走通道。**响应结构保持不变**。*依赖 4.1。*
- [x] 5.4 删除同步版 Amap 客户端：`_text_search_raw`、`search_around_pois`、`search_attraction_pois`、`fetch_forecast`。*依赖 5.1、5.2、5.3。*
- [x] 5.5 删除 `app/planning/helpers.py` 的 `fetch_city_spots` 与 `fetch_weather_for_dates`，修正 import。*依赖 5.4。*
- [x] 5.6 删除 `app/runtime/scheduler.py` 的 `amap_capacity`（死代码）与 `amap_limit` 参数；`container.py` 同步。在 `RUNTIME_AMAP_CONCURRENCY` 注释里写明真正生效点是 `app/core/async_resources.py`。*依赖 5.3。*

## 6. 缓存：只补缺失的两处

- [x] 6.1 `app/core/cache.py`：新增 `nearby_cache_key`（含坐标、半径、类型、offset、关键词）与 `walking_cache_key`（含起终点坐标对），**另起 `tripagent:nearby:` / `tripagent:walk:` 命名空间**。注释写明既有 `poi_cache_key` 不含 `types`/`offset`/`radius`，直接复用会串味。
- [x] 6.2 设定 TTL：周边搜索 2h（POI 评分与营业时间会变）、步行路线 7d（折线实质不变）。*依赖 6.1。*
- [x] 6.3 `search_around_pois_async` 接入缓存（读 + 写）。*依赖 6.1、5.1。*
- [x] 6.4 `/api/route/walking` 接入缓存。*依赖 6.1、5.3。*
- [x] 6.5 确认失败响应不写缓存（只有通道成功返回才写）。*依赖 6.3、6.4。*

## 7. 预警与可观测

- [x] 7.1 预占成功后检查是否越过预警阈值，越过且该池该月未发过则标记并发一次预警。*依赖 2.4、4.2。*
- [x] 7.2 预警事件类型 `amap.quota_warning` 加进 `app/runtime/models.py` 的 `CUSTOM_EVENT_TYPES`；经 `app/core/amap_notify.py` 的 contextvar 桥接，由 `attach_run_warning_sink` 在规划节点装上发送器。
- [x] 7.3 新增只读端点 `GET /api/runtime/amap-quota`：返回月份、`estimate: true`、说明文字与三个池的投影。*依赖 2.2。*
- [x] 7.4 新增可写端点 `POST /api/runtime/amap-quota/reconcile`：按绝对值语义录入。*依赖 2.3。*
- [x] 7.5 前端：`chat-state.js` 把预警投影成 `quota_warning` 时间线条目（排在任务卡之前、不堆叠历史），`pages.jsx` 渲染为克制的提示行，`style.css` 加 `.quota-warning`。*依赖 7.3、7.4。*（**未做**：把用量端点的数字做成独立设置面板——预警事件本身已把用量与文案带到了界面上。）

## 8. 配置与文档

- [x] 8.1 `.env.example`：新增四个变量，每个都写出官方原值与来源链接。
- [x] 8.2 `README.md`（新增第 16 节 + 环境变量表 + 第 10 节缓存说明）与 `docs/project-introduction.md`（新增「真实数据源也有真实成本」一节）。*依赖 8.1。*
- [x] 8.3 `DOCKER.md` 环境变量表、`docker-compose.yml` 与 `docker-compose.nas.yml`。*依赖 8.1。*（两份 compose 已用 YAML 解析器验证）
- [x] 8.4 `CONTEXT.md`：收录「配额池」「已用量」「用量对账」三个术语。*依赖 8.1。*
- [x] 8.5 `docs/adr/0003-amap-quota-pools-and-estimated-usage.md`。*依赖 8.1。*

## 9. 测试

- [x] 9.1 `tests/test_amap_quota.py`（39 项）：池定义、限额校验（含非法值）、月份边界（跨月 + 容器 `TZ` 变化不影响结果）、对账语义、预警只发一次。*依赖第 1、2 节。* **本机已跑通。**
- [x] 9.2 `tests/test_amap_channel.py`（19 项）：额度不足时**不发生 HTTP 请求**、重试每次都占额、`quota` 类不重试、`throttle` 类重试、名字与数字映射到同一类别、`IP_QUERY_OVER_LIMIT` 与 `INVALID_USER_KEY` 的专属 `public_code`。*依赖第 3、4 节。* **本机已跑通。**
- [x] 9.3 并发预留测试（8 线程 × 30 次请求，断言成功数恰好等于限额）：验证 `BEGIN IMMEDIATE` 的原子性确实生效。*依赖 2.2。* **本机已跑通。**
- [x] 9.4 改造 `tests/test_manual_edit_api.py`：同步调用改 async，假通道接在 `call_amap` 上，`search_city_pois_async` 的三项新增（含池归属断言）。*依赖 5.4。* **本机只跑通不依赖 FastAPI 的三项**；该文件的路由级测试 import `app.main`，需完整环境。
- [x] 9.5 改造 `tests/test_weather_mock.py`：patch 目标改为协程函数 `fetch_forecast_async`（同步替身会在 `await` 处直接报 TypeError）。*依赖 5.4。*
- [ ] 9.6 契约测试：`/api/poi/search` 与 `/api/poi/nearby` 的响应结构在改造前后一致。*依赖 5.3。* **未单独新增**：既有测试已覆盖这两个路由的响应结构，改造未改动返回字段；但需在完整环境里跑一次确认。
- [ ] 9.7 跑完整套件 `python -m pytest tests/ -q --continue-on-collection-errors`，并与基线对比新增失败。*依赖第 1–8 节。* **本机 217 passed / 37 errors**，37 个错误全部是 `langgraph`(31) + `langchain_core`(6) 缺失（与变更前同类）。完整环境仍需复跑。
- [x] 9.8 前端验证：6 个 JSX 全部通过离线编译检查；`node --test` 44 项通过（`chat-state.test.js` 42 + `navigation-state.test.js` 2，含本次新增的 2 项配额预警投影测试）。*依赖 7.5。*

## 10. 本地验证

- [x] 10.1 用假 transport 跑通"预留 → 耗尽 → 拒绝且不发请求"的完整路径。
- [x] 10.2 验证跨月边界：断言物理时刻 `2026-09-30T20:00Z` 归属 `2026-10`，且容器 `TZ` 改成 `America/New_York` / `UTC` 结果不变。
- [x] 10.3 验证已用量录入：录入绝对值后本地计数在该值之上继续累加，且不与控制台数字重复相加。
- [x] 10.4 验证预警只发一次：连续越过阈值多次，断言只产生一条。
- [x] 10.5 验证错误文案：三个池各自的超限文案含正确池名与重置时刻；IP 受限文案不含"重置"。

## 11. 待用户执行（实测与对账）

> 这几项都**无法由代码自动完成**：官方没有配额查询 API，也没有月配额超限的错误码文档。

- [ ] 11.1 **实测月配额超限的返回码。** 用一把**独立新建**的 Web 服务 Key（避免污染生产额度）打满某个池，逐字记录 `infocode` 与 `info`。
  - 建议先打 `weather` 池（5,000/月，且天气的降级路径最成熟，打满的副作用最小）。
  - 目的之一：确认"月配额超限返回哪个码"，用于补全 `QUOTA_INFOS`。
  - 目的之二：记录**打满前最后一次成功**与**第一次被拒**的请求序号，连续测两次。
- [ ] 11.2 **确认"被拒绝的请求是否计入配额消耗"。** 在控制台对比实测前后的已用量差，与本地记录的请求总数比对：若控制台差值 ≈ 总请求数（含被拒），说明被拒也计数；若 ≈ 成功数，说明不计数。
- [ ] 11.3 **确认日配额是否真的完全废止。** 官方公告说取消，但错误码表仍保留 `10003` / `10044` 且无弃用标注。在 11.1 的实测中观察是否出现这两个码。
- [ ] 11.4 **从控制台读取当前已用量并录入。** 路径：高德控制台 → 流量分析（账号维度为主，同时保留 Key 维度查询）。这是本地计数的起点，不录入则硬拦截会在真实额度用尽后再放行几百次。
- [ ] 11.5 **核对账号「1 年免费月配额」到期日。** 定价页工具提示原文："非商业目的使用的个人认证开发者……享有的1年的免费月配额量（自注册认证之日起计算）"。语义有歧义，但若为真，认证满一年后免费额度可能归零，届时本特性会立刻从"保护"变成"常态拦截"。
- [ ] 11.6 **（建议）给 Web 服务 Key 配置 IP 白名单。** `IP_QUERY_OVER_LIMIT` 的封停按官方说明"无法自动恢复，需要提交工单"，是本项目外露 Key 时风险最高的一种失败。

## 实现过程中被测试推翻并重写的两处设计

**一、对账的合并式子（唯一一处规格本身被证伪）。** 最初用单个 `counter_offset` 表达
"人工补正量"、令 `used = counter_calls + counter_offset`。测试立刻暴露它自相矛盾：控制台
读数在对账那一刻就包含了此前的本地调用，相加是重复计数；而任何"取 max"的写法又会漏掉
对账**之后**的本地增量（控制台读数是时间点快照，不可能包含未来的调用）。

改成时间锚定：`used = counter_baseline + (counter_calls - counter_at_reconcile)`。表结构加
一列 `counter_at_reconcile`，规格里对应的 requirement 与 scenario 整段重写。

**二、通道接管重试。** "重试也要占额"这条规格在最初的设计里无法成立：`http_get_json_async`
内部有一个 3 次重试循环，对调用方不可见，计数点看不到它。给该函数加了 `max_attempts`
参数，通道传 1，退避与占额都由通道驱动。

## 环境限制

本机（Windows，Python `C:\Users\HotPot\AppData\Local\Python\pythoncore-3.14-64\python.exe`）**缺少 `langgraph` 与 `langchain_core`**，因此任何 import 到 `app.chat` / `app.runtime.container` / `app.main` 的测试都无法在本机执行，会报 `ModuleNotFoundError`。

- 第 1、2、3、4、6、9.1、9.2、9.3、9.5、9.8、10 节**已在本机验证**：它们只依赖 `app.core`、`app.providers`、`app.core.database`，不触碰 LLM 栈。这也是把配额模块放在 `app/core/` 而不是 `app/runtime/repositories.py` 的附带理由——那一层能让这整块逻辑脱离 LLM 栈独立测试。
- 第 5、7、9.4（路由部分）、9.6、9.7 节需要完整环境（`requirements.txt` 全量安装）才能执行。
- 因此第 9.7 的完整套件必须在补齐依赖的环境里跑，不能只凭本机结果判定通过。

## 架构约束（不可协商）

- **不允许引入内存缓冲或定时刷盘。** 配额计数允许的失效方向是"高估"，绝不允许"低估"——崩溃丢写会低估用量，而这个特性的全部价值就在于不低估。每请求一次同步 SQLite 写入，代价可接受。
- **不允许把配额计数挂到缓存层上。** Redis 未配置时缓存静默失效，配额保护必须独立于它存在，否则会在最需要保护的环境里恰好没有保护。
- **不允许合并午晚餐搜索。** 那会改变候选池构成，属于影响规划质量的改动，与本变更目标不同源。
