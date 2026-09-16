# FloatTrip 飞牛 NAS 部署指南

本文档说明如何在飞牛 NAS（fnOS）上部署 FloatTrip。相比本机开发部署，NAS 场景的差异是：
**不需要源码构建，直接拉取 Docker Hub 上的现成镜像**。

---

## 一、前置条件

| 项目 | 要求 |
| --- | --- |
| CPU 架构 | **x86_64 / AMD64**（Intel、AMD 处理器，飞牛主流机型均满足） |
| Docker | 飞牛自带 Docker，确认「Docker」应用已安装并启动 |
| 网络 | NAS 能访问 Docker Hub（国内网络建议配置镜像加速） |
| 密钥 | 高德【Web 服务】Key + DeepSeek API Key，见第三节 |

> **ARM 机型请注意**：当前镜像只提供 `linux/amd64`。若你的飞牛是 ARM 处理器
> （如瑞芯微 RK3588），需要另行构建 arm64 镜像。

在 NAS 上确认架构的方法（SSH 执行）：

```bash
uname -m
# 输出 x86_64 → 可直接使用；输出 aarch64 → 需要 arm64 镜像
```

---

## 二、部署步骤

配置文件为仓库根目录的 **`docker-compose.nas.yml`**。

### 方式 A：飞牛 Docker 界面（推荐）

1. 下载或复制 `docker-compose.nas.yml` 的全部内容
2. 打开飞牛「**Docker**」→「**Compose**」→「**新建项目**」
3. 项目名称填 `floattrip`
4. 把文件内容粘贴到编辑框，**修改第三节的两个密钥**
5. 点击「部署」，等待镜像拉取完成（首次约 400 MB，视网速需几分钟）
6. 浏览器访问 `http://<NAS的IP>:8765`

### 方式 B：SSH 命令行

```bash
# 1. 上传 docker-compose.nas.yml 到 NAS，例如：
mkdir -p /vol1/1000/docker/floattrip
cd /vol1/1000/docker/floattrip
# （用 scp 或飞牛文件管理器把文件放进来）

# 2. 编辑密钥
vi docker-compose.nas.yml

# 3. 启动
docker compose -f docker-compose.nas.yml up -d

# 4. 查看日志
docker compose -f docker-compose.nas.yml logs -f
```

> 目录路径按你的实际存储空间调整。飞牛的存储空间通常挂载在 `/vol1`、`/vol2` 下。

---

## 三、必填密钥

编辑 `docker-compose.nas.yml` 的 `environment` 段，**替换掉这两个占位符**：

```yaml
AMAP_API_KEY: "<在此填写高德Web服务Key>"        # ← 替换为真实值
DEEPSEEK_API_KEY: "<在此填写DeepSeek的Key>"      # ← 替换为真实值
```

| 密钥 | 获取方式 |
| --- | --- |
| `AMAP_API_KEY` | [高德开放平台](https://lbs.amap.com/) → 控制台 → 应用管理 → 创建应用 → 添加 Key → 服务平台选 **Web 服务** |
| `DEEPSEEK_API_KEY` | [DeepSeek 平台](https://platform.deepseek.com/) → API Keys → 创建 |

> ⚠️ **高德 Key 类型别选错**：必须选「**Web 服务**」，不能选「Web端(JS API)」。
> 填错时容器仍能启动，但发起规划请求会失败。

> 💡 入口脚本会识别「填写 / 你的 / xxx」等占位文本并**拒绝启动**，
> 避免用无效密钥跑起来、直到请求时才报难以定位的错误。

### 可选配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_PROVIDER` | `deepseek` | 改为 `doubao` 使用豆包，需同时填 `DOUBAO_API_KEY` |
| `AMAP_JS_KEY` | 空 | 高德 Web端(JS API) Key，用于前端地图可视化，不填则地图降级 |
| `AMAP_JS_SECURITY_CODE` | 空 | 与 JS Key 配套的安全密钥 |
| `TZ` | `Asia/Shanghai` | 容器时区，影响日志时间显示 |
| `REDIS_URL` | 空 | 留空自动禁用缓存；启用 Redis 时改为 `redis://redis:6379/0` |
| `AMAP_QUOTA_SEARCH_LIMIT` | `4750` | 基础搜索服务月配额（官方 5000）。`0` = 不限制 |
| `AMAP_QUOTA_WEATHER_LIMIT` | `4750` | 天气预报月配额（官方 5000）。`0` = 不限制 |
| `AMAP_QUOTA_LBS_LIMIT` | `0` | 基础LBS服务月配额。默认不限制（官方 150000） |
| `AMAP_QUOTA_WARNING_RATIO` | `0.8` | 配额预警阈值比例 |

> ⚠️ **配额保护默认开启，且额度不足时程序会直接拒绝请求、一次都不发出。**
>
> 高德自 2025-05-20 起取消日配额、改为按「服务组」共享的**月配额**：同一服务组内所有接口、同一账号下所有 Key（含前端 JS API Key）合计消耗同一份额度。个人认证开发者的**基础搜索服务只有 5,000/月**，而一次 5 天单城市规划就要消耗约 13 次搜索（3 次景点 + 10 次餐饮周边）。
>
> **不要为了"让它能跑"而把限额调高或设为 0** —— 那只是把超额推迟到高德那边，届时整个账号的该服务组都会被拒绝，影响范围比本应用更大。
>
> 本地计数是**估算**：高德没有配额查询接口，计数从部署那一刻从 0 开始，看不到此前的消耗。NAS 上可用 `curl` 录入控制台读数对账（`bucket` 取 `search` / `weather` / `lbs`，`used` 填**绝对值**）：
>
> ```bash
> curl -s -X POST http://<NAS地址>:8765/api/runtime/amap-quota/reconcile \
>   -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
>   -d '{"bucket": "search", "used": 1234}'
> ```
>
> 只读查看：`GET /api/runtime/amap-quota`。控制台里的「基础地图定位服务」不计入本项目的任何池（那是前端地图直接消费的），不需要录入。

---

## 四、数据持久化

配置使用 **Docker 命名卷** `floattrip-data`，挂载到容器内 `/app/data`，
存放 SQLite 数据库（用户、规划历史、LangGraph checkpoint）。

- 卷的实际名称带项目名前缀，例如 `floattrip_floattrip-data`
- 容器重建、镜像升级后数据均保留
- 升级镜像：`docker compose -f docker-compose.nas.yml pull && docker compose -f docker-compose.nas.yml up -d`

备份数据（SSH 执行）：

```bash
docker run --rm -v floattrip_floattrip-data:/data -v "$PWD:/backup" \
  alpine tar czf /backup/floattrip-data-$(date +%F).tar.gz -C /data .
```

---

## 五、常用运维命令

```bash
cd /vol1/1000/docker/floattrip        # 换成你的实际目录

# 查看状态
docker compose -f docker-compose.nas.yml ps

# 查看日志（实时）
docker compose -f docker-compose.nas.yml logs -f

# 重启
docker compose -f docker-compose.nas.yml restart

# 停止并删除容器（数据卷保留）
docker compose -f docker-compose.nas.yml down

# 更新到最新镜像
docker compose -f docker-compose.nas.yml pull
docker compose -f docker-compose.nas.yml up -d
```

---

## 六、故障排查

### 1. 状态显示 `Restarting`，日志反复输出「启动中止」

密钥仍是占位符或为空。按第三节替换真实密钥后：

```bash
docker compose -f docker-compose.nas.yml down
docker compose -f docker-compose.nas.yml up -d
```

> `restart: unless-stopped` 会让配置错误的容器反复重启，这是正常现象 ——
> 修正配置或先 `down` 停止即可。

### 2. 拉取镜像很慢或超时

国内网络建议给飞牛的 Docker 配置镜像加速：飞牛「Docker」→「设置」→「镜像加速」，
填入加速地址后重试。

### 3. 端口 8765 被占用

修改 compose 中的端口映射，例如改成 `8876:8765`，然后访问 `http://<NAS的IP>:8876`。
注意容器内端口 `8765` 不要改。

### 4. 页面能打开，但发起规划请求报错

密钥已传入但值不可用，常见原因：

- 高德 Key 选成了「Web端(JS API)」而非「Web 服务」
- 密钥额度用尽或未实名认证
- YAML 中密钥被多余的引号包裹（正常写法：`AMAP_API_KEY: "真实key"`）

### 5. 容器健康但无法从其他设备访问

飞牛防火墙或路由器隔离。检查飞牛的防火墙设置是否放行 8765 端口，
并确认手机/电脑与 NAS 在同一网段。

### 6. 想确认容器架构是否匹配

```bash
docker inspect hotpot1993/floattrip:latest --format '{{.Architecture}}'
# 应输出 amd64
```

---

## 七、与本机 compose 文件的区别

| 文件 | 用途 | 差异 |
| --- | --- | --- |
| `docker-compose.yml` | 本机开发 / 有源码的服务器 | 含 `build` 段从源码构建；依赖 `.env` 文件；数据挂载到 `./data` |
| `docker-compose.nas.yml` | **飞牛 NAS 部署** | 直接拉取 Docker Hub 镜像；密钥写在配置内；数据用命名卷 |

两者的镜像完全相同（`hotpot1993/floattrip:latest`），只是编排方式针对各自环境做了优化。
