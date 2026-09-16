# 高德配额保护

## 问题

### 一、程序对额度消耗完全无感知

后端共有 4 个高德 REST 出口、8 处真实请求点，**没有任何一处计数**。全仓库搜 `quota` / `限额` / `配额` / `daily` 零命中。因此额度耗尽永远以"意外"的形式出现，且在事后没有任何记录可回溯。

### 二、额度耗尽时程序会主动加剧耗尽

`app/providers/amap/client.py:10-14` 的 `AMAP_RATE_LIMIT_INFOS` 把配额类错误归入"可重试"：

- 高德层（`poi.py:26` / `:39` / `:81` / `:117`）退避后重试，最多 4 次；
- HTTP 层（`app/core/http.py:57` / `:101`）再重试最多 3 次。

两者相乘：**一次已经超额的逻辑调用最坏会变成 12 个真实 HTTP 请求**。也就是说，程序在额度见底时的行为是加倍索取，而不是停下来。

### 三、用户看不到原因

Run 内命中配额错误时，`RuntimeError` 冒泡到 `app/runtime/scheduler.py:168-189`，该处读取 `getattr(exc, "public_code", "run_failed")` / `getattr(exc, "public_message", ...)`。裸 `RuntimeError` 没有这两个属性，于是用户只看到 **"任务执行失败，请稍后重试"**。用户会反复重试，而每次重试都在继续消耗（已耗尽的）额度。

### 四、额度的维度变了，代码的假设已经过期

高德已于 2025-05-20 取消日配额、改为按「服务组」共享的**月配额**：

> 取消各组基础服务的现有日配额限制，同时现有的每组基础服务的月配额保持不变。若开发者在一日内调用任何一组基础服务的累计次数达到了该组基础服务所共享的月配额上限的，则开发者需要付费购买流量才可继续使用该组基础服务。

本项目落在三个相互独立的池里（已认证个人开发者）：

| 池 | 本项目用途 | 月配额 | 折算日均 |
| --- | --- | --- | --- |
| 基础搜索服务 | `place/text` + `place/around` | 5,000 | ≈164 |
| 其它基础服务-天气预报 | `weather/weatherInfo` | 5,000 | ≈164 |
| 基础LBS服务 | `direction/walking`（后端）+ `AMap.Driving`（前端） | 150,000 | ≈5,000 |

**最紧的是搜索池。** 一次 5 天单城市规划 = 3 次景点搜索 + 10 次餐饮周边搜索（每天午晚餐各一次，见 `app/planning/nodes.py:645-669`）= **13 次**，占月额度的 0.26%。满打满算一个月约可规划 384 次。而搜索池的另一个特性是"额度按账号共享"：官方 FAQ 明确「同账号下Key之间的配额是共用」，定价页在每个服务组都标注「配额共享平台：API、JS、Android、iOS、微信小程序」。

## 目标

在不超支的前提下保住可用性：**宁可提前停，也不要撞上高德之后再去处理。**

## 范围

### 覆盖

- **只覆盖后端 `AMAP_API_KEY`** 的 4 个 REST 出口：`place/text`、`place/around`、`weather/weatherInfo`、`direction/walking`。
- 三个池各自独立限额、独立预警、独立重置。
- 每次请求发出前先占额；额度不足时立即拒绝，不发出网络请求。
- 错误按语义分三类，配额类不可重试。
- 补齐周边搜索与步行路线的缓存。
- 用户可见的失败原因与重置时刻。
- 达阈值预警一次 + 只读用量端点。

### 不覆盖（明确出界）

- **前端 JS API Key**。`AMap.Map` / `AMap.Driving` 由浏览器直接消费，服务端数不到。`AMap.Driving` 消耗的是 LBS 池（150,000/月，充裕），且**前端当前没有使用任何 JS 侧 POI 搜索插件**（已核对：只用 `AMap.Map` / `Driving` / `Walking` / `Polyline` / `Marker` / `InfoWindow`），因此不蚕食紧张的 5,000 搜索池。将来若有人引入 JS 搜索插件，会静默蚕食该池——这是本变更遗留的**已知缺口**，堵住它需要把 JS 侧调用也代理到后端。
- **`mobile-app`** 的 iOS/Android 高德 Key。
- **午晚餐搜索合并**。合并会改变候选池构成，属于影响规划质量的改动，与本变更的"额度保护"目标不同源。
- **城市级长 TTL 景点缓存**。本次只补真正缺失的两处缓存，不引入跨月保留的 POI 陈旧化问题。
- **自动读取控制台已用量**。官方没有配额查询 API（已逐一核对全部 Web 服务文档、CLI 与 MCP Server 文档，确认不存在），只能人工录入。

## 方案要点

1. **收敛为单一「高德 REST 调用通道」**（`amap-rest-call-channel`）。把 `direction/walking` 的 URL 从 `app/api/plan_routes.py:595` 的内联字面量收进 `client.py`，让三条同步路由（`/api/poi/search`、`/api/poi/nearby`、`/api/route/walking`）也走这条通道。闸门只有在成为唯一出入口时才有意义。
2. **按池预留**（`amap-quota-guard`）。三个池各自的月配额与已用量分池记录，计数口径是**实际发出的 HTTP 请求数**（不是逻辑调用数），因为只有它能在重试放大下保持正确。
3. **已用量可人工对账**。本地计数从 0 起步，而真实已用量可能已有数百次。加一个"设置本月已用绝对值"的录入能力，让用户从控制台读到数字后写入。
4. **错误三分**（`amap-rest-call-channel`）。配额类立即失败且不重试；限流类退避重试；其余失败按原语义处理。
5. **原因送达 UI**（`planning-task-presentation`）。异常携带 `public_code` / `public_message`，复用调度器已有的属性读取路径，文案含重置时刻。
6. **缓存止损**（`amap-rest-call-channel`）。周边搜索与步行路线建独立缓存键；缓存命中不占额。

## 影响

### 新增

- `app/core/amap_quota.py`（池定义、限额、预留、对账）
- `app/providers/amap/channel.py`（唯一出口：错误分类 + 占额 + 请求）
- `app/runtime/repositories.py` 中的用量仓储与对应表
- `tests/test_amap_quota.py`、`tests/test_amap_channel.py`

### 修改

- `app/core/database.py`：新增用量表 + 逐条 ALTER 迁移保护
- `app/providers/amap/client.py`：错误三分集合、`direction/walking` URL 常量
- `app/providers/amap/poi.py`：周边搜索走通道并加缓存
- `app/providers/weather/amap.py`：走通道
- `app/api/plan_routes.py`：三条路由改走通道；步行路线加缓存
- `app/api/runtime_routes.py`：只读用量端点
- `app/runtime/scheduler.py`：删除无效的 `amap_capacity`
- `app/core/cache.py`：新增周边搜索与步行路线的缓存键与 TTL
- `frontend/`：用量展示与预警提示

### 删除

- `app/planning/helpers.py` 的同步版 `fetch_city_spots` / `fetch_weather_for_dates`，以及同步版 `search_attraction_pois` / `search_around_pois`（生产路径已无调用者，仅测试引用；且若被误启用会在事件循环里阻塞——urllib + `time.sleep`）。

### 需要用户执行

- 从控制台读取当前已用量并录入，作为本地计数的起点。
- 核对账号「1 年免费月配额」到期日（定价页工具提示："非商业目的使用的个人认证开发者……享有的1年的免费月配额量（自注册认证之日起计算）"。语义有歧义，但若为真，认证满一年后免费额度可能归零，届时本特性会立刻从"保护"变成"常态拦截"）。
- 执行一次对真实 Key 的实测，确认月配额超限的返回码。

### 外部约束（不可协商）

1. **官方没有配额查询 API**。官方唯一指引是"QPS 配额信息，请在 控制台-流量分析-配额管理 页面查看"。因此本特性是**估算**，不是对账。必须按这个定位写文档与文案，否则会给出虚假的确定性。
2. **"1 年的免费月配额量"文本语义未确认**。
3. **QPS 数值官方不公开**，只能登录控制台查看。
4. **月配额的超限返回码未文档化**。官方错误码表只有日调用量的 10003 / 10044，没有月配额条目。
