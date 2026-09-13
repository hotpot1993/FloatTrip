#!/bin/sh
# FloatTrip 容器入口脚本
#
# 职责：在启动应用前保证数据目录存在且可写。
# 说明：容器可能以只读根文件系统或挂载数据卷的方式运行，因此 data/ 的可用性
#       需要在每次启动时确认，而不能只在镜像构建阶段创建。

set -e

# 数据目录：SQLite 数据库（app.db）与 LangGraph checkpoint 的落盘位置
DATA_DIR="${APP_HOME:-/app}/data"

if [ ! -d "$DATA_DIR" ]; then
    echo "[entrypoint] 数据目录不存在，正在创建：$DATA_DIR"
    mkdir -p "$DATA_DIR" 2>/dev/null || true
fi

if [ ! -d "$DATA_DIR" ]; then
    echo "[entrypoint] 错误：无法创建数据目录 -> $DATA_DIR" >&2
    echo "[entrypoint] 请检查挂载路径是否正确，或改用命名卷（named volume）。" >&2
    exit 1
fi

# 实际写入一个探针文件，比权限位判断更可靠（尤其针对网络文件系统与挂载卷）
if ! touch "$DATA_DIR/.write-test" 2>/dev/null; then
    echo "[entrypoint] 错误：数据目录不可写 -> $DATA_DIR" >&2
    echo "[entrypoint] 说明：容器内以 UID 10001 运行，宿主机目录需对该 UID 可写。" >&2
    echo "[entrypoint] Linux 宿主可执行：sudo chown -R 10001:10001 ./data" >&2
    echo "[entrypoint] 或改用命名卷，例如：-v floattrip-data:/app/data" >&2
    exit 1
fi
rm -f "$DATA_DIR/.write-test"

# ─────────────────────────── 环境变量预检 ───────────────────────────
# 背景：应用在启动阶段就会构建 LLM 客户端，缺少密钥会直接抛错退出
#       （RuntimeError: 缺少 DEEPSEEK_API_KEY）。应用自身的报错文案面向本地开发
#       （提示配置 .env.local），容器用户看到会困惑，因此在此提前拦截，
#       并给出 --env-file / -e 在容器场景下的正确用法。

missing_amap=""
missing_llm=""
missing_llm_name=""

# 高德地图 Key 为必需项（Web 服务 Key，用于 POI 搜索与天气查询）
if [ -z "${AMAP_API_KEY:-}" ]; then
    missing_amap=1
fi

# LLM 提供商的必需密钥随 LLM_PROVIDER 变化，默认 deepseek
case "$(printf '%s' "${LLM_PROVIDER:-deepseek}" | tr '[:upper:]' '[:lower:]')" in
    deepseek)
        if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
            missing_llm=1
            missing_llm_name="DEEPSEEK_API_KEY"
        fi
        ;;
    doubao)
        if [ -z "${DOUBAO_API_KEY:-}" ]; then
            missing_llm=1
            missing_llm_name="DOUBAO_API_KEY"
        fi
        ;;
esac

if [ -n "$missing_amap" ] || [ -n "$missing_llm" ]; then
    echo "" >&2
    echo "================================================================" >&2
    echo " 启动中止：缺少必需的环境变量" >&2
    echo "================================================================" >&2
    echo "" >&2
    echo " 缺失项：" >&2
    if [ -n "$missing_amap" ]; then
        echo "   - AMAP_API_KEY      高德【Web 服务】Key（注意不是 JS API Key）" >&2
    fi
    if [ -n "$missing_llm" ]; then
        echo "   - ${missing_llm_name}  LLM API Key（当前 LLM_PROVIDER=${LLM_PROVIDER:-deepseek}）" >&2
    fi
    echo "" >&2
    echo " 容器中请用环境变量注入；镜像内不存在也不应存在 .env.local。" >&2
    echo "" >&2
    echo " 推荐做法（在项目根目录先执行 cp .env.example .env 并填写）：" >&2
    echo "   docker run -d --name floattrip -p 8765:8765 \\" >&2
    echo "     --env-file .env -v \"\${PWD}/data:/app/data\" \\" >&2
    echo "     hotpot1993/floattrip:latest" >&2
    echo "" >&2
    echo " 或用 -e 直接指定：" >&2
    echo "   docker run -d -p 8765:8765 \\" >&2
    echo "     -e AMAP_API_KEY=你的高德Web服务Key \\" >&2
    echo "     -e DEEPSEEK_API_KEY=你的DeepSeekKey \\" >&2
    echo "     hotpot1993/floattrip:latest" >&2
    echo "" >&2
    echo " 使用 docker compose 时，确认项目根目录存在 .env 文件后执行：" >&2
    echo "   docker compose up -d" >&2
    echo "================================================================" >&2
    echo "" >&2
    exit 1
fi

echo "[entrypoint] 数据目录就绪：$DATA_DIR"
echo "[entrypoint] 环境变量检查通过"
echo "[entrypoint] 启动命令：$*"

# 执行 CMD 传入的命令（默认：python run.py，监听 0.0.0.0:8765）
exec "$@"
