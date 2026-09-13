# FloatTrip AI 旅游规划助手 —— 生产镜像
#
# 构建特性：
#   1. 单镜像同时包含 FastAPI 后端与前端静态资源（frontend/ 由后端直接托管）
#   2. 依赖先于源码拷贝安装，源码改动可复用 pip 缓存层，二次构建显著加速
#   3. 以非 root 用户运行，降低容器逃逸风险
#   4. 内置健康检查，便于编排工具感知服务状态
#
# 构建命令：docker build -t hotpot1993/floattrip:latest .

# ───────────────────────────── 基础镜像 ─────────────────────────────
# 选用 3.12-slim：项目要求 Python 3.10+，3.12 为当前依赖生态（langgraph /
# langchain / pydantic v2）wheel 覆盖最完整的稳定版本，且镜像体积可控。
FROM python:3.12-slim

# ───────────────────────────── 运行环境变量 ─────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_HOME=/app \
    APP_PORT=8765

# ───────────────────────────── 系统依赖 ─────────────────────────────
# curl：供 HEALTHCHECK 探测使用
# 说明：requirements.txt 中的依赖均为 manylinux 预编译 wheel，
#       无需 gcc 等编译工具链，因此不安装 build-essential 以压缩镜像体积。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR ${APP_HOME}

# ───────────────────────────── 安装 Python 依赖 ─────────────────────────────
# 单独拷贝依赖清单，充分利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# ───────────────────────────── 拷贝应用源码 ─────────────────────────────
COPY app/ ./app/
COPY frontend/ ./frontend/
COPY run.py ./
# 环境变量模板：供容器内查看可配置项，真实密钥请通过运行时 -e / --env-file 注入
COPY .env.example ./.env.example

# 入口脚本：负责首次启动时准备可写数据目录
# 其中 sed 用于兜底清除可能存在的 CRLF 换行（Windows 检出场景），
# 否则容器内 /bin/sh 会因 "$'\r': command not found" 启动失败。
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh

# ───────────────────────────── 用户与权限 ─────────────────────────────
# 创建非 root 用户；data/ 为 SQLite 数据库与 LangGraph checkpoint 的持久化目录，
# 需提前创建并授权，避免容器以只读根文件系统运行时启动失败。
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && useradd --create-home --shell /bin/bash --uid 10001 floattrip \
    && mkdir -p ${APP_HOME}/data \
    && chown -R floattrip:floattrip ${APP_HOME}

USER floattrip

# ───────────────────────────── 网络与健康检查 ─────────────────────────────
EXPOSE 8765

# 依赖应用自带的 /api/health 探针
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/api/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "run.py"]
