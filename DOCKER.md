# FloatTrip Docker 部署指南

本文档说明如何把 FloatTrip 打包为 Docker 镜像，并推送到 Docker Hub 供他人拉取运行。

---

## 一、前置条件

### 1. 安装 Docker

本机当前**未安装任何容器运行时**，需要先安装：

```powershell
winget install --id Docker.DockerDesktop -e
```

安装完成后：

1. 启动 **Docker Desktop**，等待托盘图标变为绿色（引擎就绪）
2. **重启终端**，执行 `docker version` 确认能同时输出 Client 与 Server 版本

> 仅有 Client 版本输出说明守护进程未启动；`docker` 命令找不到说明终端未刷新 PATH 或安装未完成。

### 2. 准备 Docker Hub 账号与仓库

1. 登录 [Docker Hub](https://hub.docker.com/)（无账号则先注册）
2. 创建仓库：**Create repository** → 名称填 `floattrip` → 可见性建议 **Public**
3. 生成访问令牌：**Account Settings → Personal access tokens → Generate new token**

> **重要**：Docker Hub 已要求使用访问令牌（Access Token）代替账号密码进行 `docker login`，
> 令牌只在生成时显示一次，请立即保存。

最终推送目标为 `hotpot1993/floattrip:latest`（如你的 Docker Hub 用户名不同，见第六节自定义）。

---

## 二、准备密钥配置

应用依赖高德地图与 LLM 服务，密钥通过环境变量注入，**不会打进镜像**。

```powershell
# 在项目根目录执行
Copy-Item .env.example .env
```

编辑 `.env`，至少填写以下两项：

| 变量 | 是否必填 | 说明 |
| --- | --- | --- |
| `AMAP_API_KEY` | **必填** | 高德 **Web 服务** Key，用于 POI 搜索与天气查询 |
| `DEEPSEEK_API_KEY` | **必填** | DeepSeek API Key，用于行程规划推理 |
| `AMAP_JS_KEY` | 可选 | 高德 **Web 端(JS API)** Key，用于前端地图可视化，缺失则地图降级 |
| `AMAP_JS_SECURITY_CODE` | 可选 | 与 JS Key 配套的安全密钥 |
| `REDIS_URL` | 可选 | 留空则自动禁用缓存，不影响主流程 |

> `.env` 已被 `.gitignore` 与 `.dockerignore` 双重排除，不会提交仓库，也不会进入镜像。

---

## 三、构建并推送（推荐方式）

### Windows PowerShell

```powershell
# 交互式：构建后询问是否推送
.\build.ps1

# 只构建，本地验证 Dockerfile
.\build.ps1 -NoPush

# 构建并直接推送
.\build.ps1 -Push

# 指定平台与版本标签（服务器多为 x86_64）
.\build.ps1 -Platform linux/amd64 -Tag v1.0.0 -Push
```

### Linux / macOS

```bash
chmod +x build.sh
./build.sh --push
```

脚本会依次完成：检查 Docker 环境 → 校验构建上下文 → 构建镜像 → 登录 Docker Hub → 推送，
任一步失败立即中止并给出中文原因提示。

### 手动等价命令

```powershell
docker build -t hotpot1993/floattrip:latest .
docker login
docker push hotpot1993/floattrip:latest
```

---

## 四、运行镜像

> **重要：密钥必须随容器启动一起提供**
>
> 应用在**启动阶段**就会构建 LLM 客户端，因此缺少 `AMAP_API_KEY` 或 LLM Key 时，
> 容器会直接启动失败并退出（`Application startup failed. Exiting.`），而不是等到
> 发起规划请求时才报错。
>
> 入口脚本会在启动前预检这两项，缺失时打印中文指引（含缺失项与 `--env-file` 示例）
> 后以退出码 1 结束。用 `docker logs <容器名>` 即可看到指引。

### 方式一：docker run

```powershell
docker run -d --name floattrip -p 8765:8765 --env-file .env -v "${PWD}/data:/app/data" hotpot1993/floattrip:latest
```

浏览器访问 **http://localhost:8765**。

### 方式二：docker compose（含可选 Redis）

```powershell
# 基础启动
docker compose up -d

# 附带 Redis 缓存（同时把 compose 中 REDIS_URL 改为 redis://redis:6379/0）
docker compose --profile cache up -d

# 查看日志与健康状态
docker compose logs -f
docker compose ps
```

### 拉取他人已推送的镜像

```powershell
docker pull hotpot1993/floattrip:latest
docker run -d --name floattrip -p 8765:8765 --env-file .env hotpot1993/floattrip:latest
```

---

## 五、镜像设计说明

| 项目 | 取值 | 设计理由 |
| --- | --- | --- |
| 基础镜像 | `python:3.12-slim` | 项目要求 Python 3.10+，3.12 的依赖 wheel 覆盖最完整且体积可控 |
| 监听端口 | `8765` | 与 `run.py` 中 uvicorn 配置保持一致 |
| 前端资源 | 打包进同一镜像 | `frontend/` 由 FastAPI 以 `StaticFiles` 挂载，无需独立前端服务 |
| 前端构建 | 无需构建步骤 | 页面通过 CDN 的 Babel standalone 在浏览器端编译 JSX，静态文件即可运行 |
| 运行用户 | `floattrip`（UID 10001） | 非 root 运行，降低容器逃逸风险 |
| 数据目录 | `/app/data` | 存放 `app.db` 与 `langgraph-checkpoints.db`，需挂载卷持久化 |
| 健康检查 | `GET /api/health` | 使用应用自带探针，便于编排工具感知状态 |
| 入口预检 | 检查数据目录可写 + 必需密钥 | 把容器场景的配置问题在启动前用中文说清，避免用户面对面向本地开发的报错 |
| 依赖安装顺序 | 先 `requirements.txt` 后源码 | 源码改动可复用依赖层缓存，二次构建显著加速 |

镜像内**不包含**：`.env` 密钥、SQLite 运行数据、`tests/`、`docs/`、移动端工程、`node_modules`。

---

## 六、自定义镜像名与标签

如果 Docker Hub 用户名不是 `hotpot1993`：

```powershell
# 方式一：脚本参数
.\build.ps1 -Image "你的用户名/floattrip" -Tag latest -Push

# 方式二：手动打标签后推送
docker build -t floattrip:latest .
docker tag floattrip:latest 你的用户名/floattrip:latest
docker push 你的用户名/floattrip:latest
```

使用 compose 时，同步修改 `docker-compose.yml` 中的 `image:` 字段。

---

## 七、常见问题排查

### 1. `docker: The term 'docker' is not recognized`

Docker Desktop 未安装或终端未刷新 PATH。安装后**重启终端**，或直接使用完整路径
`C:\Program Files\Docker\Docker\resources\bin\docker.exe`。

### 2. `error during connect: ... The system cannot find the file specified`

Docker 守护进程未运行。启动 Docker Desktop 并等待引擎就绪。

### 3. 构建时 pip 安装缓慢或超时

国内网络环境可配置 pip 镜像源。在 `Dockerfile` 的 pip 安装步骤前插入：

```dockerfile
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

### 4. 推送报 `denied: requested access to the resource is denied`

- Docker Hub 中尚未创建 `floattrip` 仓库 → 先在网页端创建
- 登录账号与镜像名前缀不一致 → 确认 `docker login` 用的用户名
- 使用账号密码而非访问令牌登录 → 改用 Access Token

### 5. 容器内报 `$'\r': command not found`

Windows 检出把 `entrypoint.sh` 转成了 CRLF。本项目已通过 `.gitattributes` 固定为 LF，
并在 `Dockerfile` 中用 `sed -i 's/\r$//'` 兜底，正常情况不会出现。

### 6. 容器启动即退出（最常见）

```powershell
docker logs <容器名>
```

日志若显示 `启动中止：缺少必需的环境变量`，按提示补齐后重启容器。
应用在启动阶段就构建 LLM 客户端，因此缺少密钥会直接退出，而不是等到请求时才报错。

若日志显示 `Application startup failed. Exiting.` 且伴随 `RuntimeError: 缺少 ... API_KEY`，
说明密钥未传入容器 —— 检查是否漏了 `--env-file .env`，或 `-e` 变量名拼写有误。

### 7. 页面能打开但规划请求失败

密钥已传入但值不可用。常见原因：`AMAP_API_KEY` 填成了 JS API Key
（应填 **Web 服务** Key）、`.env` 中变量值被引号包裹、或额度已用尽。

### 8. 数据在容器重建后丢失

`/app/data` 未挂载宿主机目录。运行命令或 compose 中必须包含
`-v "${PWD}/data:/app/data"` 这类挂载，详见第四节。

### 9. 挂载卷权限错误（Linux 宿主）

容器内以 UID 10001 运行，需保证宿主目录可写：

```bash
sudo chown -R 10001:10001 ./data
```

---

## 八、交付文件清单

| 文件 | 作用 |
| --- | --- |
| `Dockerfile` | 镜像构建定义 |
| `.dockerignore` | 构建上下文排除规则，防止密钥与数据入镜像 |
| `docker-compose.yml` | 一键编排，含可选 Redis 与数据卷 |
| `docker-compose.nas.yml` | **飞牛 NAS 专用**编排：直接拉取镜像、密钥内联、命名卷 |
| `DOCKER-NAS.md` | 飞牛 NAS 部署指南（部署步骤、密钥、备份、排查） |
| `build.ps1` | Windows 构建推送脚本（中文提示、失败中止） |
| `build.sh` | Linux/macOS 构建推送脚本 |
| `docker/entrypoint.sh` | 容器入口：启动前校验数据目录可写，并预检必需密钥（缺失时给出中文指引） |
| `.gitattributes` | 强制 shell 脚本使用 LF 换行 |
| `DOCKER.md` | 本文档 |
