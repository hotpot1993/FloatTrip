#!/usr/bin/env bash
# FloatTrip 镜像构建与推送脚本（Linux / macOS 版）
#
# 用法：
#   ./build.sh                    # 交互式：构建后询问是否推送
#   ./build.sh --push             # 构建并推送
#   ./build.sh --no-push          # 只构建，本地验证
#   ./build.sh --tag v1.0.0       # 指定标签
#   ./build.sh --image user/repo  # 指定镜像仓库
#   ./build.sh --platform linux/amd64
#
# 说明：脚本对每一步都做失败检查，任一步出错立即中止，避免产生半成品推送。

set -euo pipefail

IMAGE="hotpot1993/floattrip"
TAG="latest"
PLATFORM=""
MODE="ask" # ask | push | no-push

# ─────────────────────── 解析参数 ───────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --image)     IMAGE="$2"; shift 2 ;;
        --tag)       TAG="$2"; shift 2 ;;
        --platform)  PLATFORM="$2"; shift 2 ;;
        --push)      MODE="push"; shift ;;
        --no-push)   MODE="no-push"; shift ;;
        -h|--help)
            sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "[错误] 未知参数：$1" >&2
            exit 1 ;;
    esac
done

FULL_IMAGE="${IMAGE}:${TAG}"

log_step() { printf '\n==== %s ====\n' "$1"; }
log_ok()   { printf '[成功] %s\n' "$1"; }
log_warn() { printf '[提示] %s\n' "$1"; }
log_err()  { printf '[错误] %s\n' "$1" >&2; }

# ─────────────────────── 步骤 1：检查 Docker 环境 ───────────────────────
log_step "步骤 1/5：检查 Docker 环境"

if ! command -v docker >/dev/null 2>&1; then
    log_err "未找到 docker 命令，请先安装 Docker Engine 或 Docker Desktop。"
    exit 1
fi

if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    log_err "Docker 守护进程未运行，请启动后重试。"
    exit 1
fi
log_ok "Docker 守护进程已就绪，服务端版本：$(docker version --format '{{.Server.Version}}')"

for required in Dockerfile requirements.txt run.py; do
    if [[ ! -f "$required" ]]; then
        log_err "当前目录缺少 ${required}，请在项目根目录（FloatTrip）下运行本脚本。"
        exit 1
    fi
done
log_ok "构建上下文校验通过：$(pwd)"

# ─────────────────────── 步骤 2：确认操作范围 ───────────────────────
log_step "步骤 2/5：确认操作范围"
echo "目标镜像：${FULL_IMAGE}"

case "$MODE" in
    no-push) log_warn "已指定 --no-push，本次仅构建，不推送。" ;;
    push)    : ;;
    ask)
        read -r -p "构建完成后是否推送到 Docker Hub？(y/N) " answer
        case "$answer" in
            y|Y) MODE="push" ;;
            *)   MODE="no-push" ;;
        esac
        ;;
esac

# ─────────────────────── 步骤 3：构建镜像 ───────────────────────
log_step "步骤 3/5：构建镜像"

build_args=(build -t "$FULL_IMAGE")
if [[ -n "$PLATFORM" ]]; then
    build_args+=(--platform "$PLATFORM")
fi
build_args+=(.)

echo "执行：docker ${build_args[*]}"
if ! docker "${build_args[@]}"; then
    log_err "镜像构建失败，请查看上方构建日志定位问题。"
    exit 1
fi
log_ok "镜像构建完成：${FULL_IMAGE}"

# ─────────────────────── 步骤 4：登录 Docker Hub ───────────────────────
if [[ "$MODE" != "push" ]]; then
    log_step "完成"
    log_ok "已跳过推送。本地运行验证命令："
    echo "  docker run --rm -p 8765:8765 --env-file .env ${FULL_IMAGE}"
    exit 0
fi

log_step "步骤 4/5：登录 Docker Hub"
echo "即将执行 docker login，请使用 Docker Hub 用户名与访问令牌（Access Token）。"
if ! docker login; then
    log_err "Docker Hub 登录失败，推送已中止。"
    exit 1
fi
log_ok "Docker Hub 登录成功"

# ─────────────────────── 步骤 5：推送镜像 ───────────────────────
log_step "步骤 5/5：推送镜像到 Docker Hub"

echo "执行：docker push ${FULL_IMAGE}"
if ! docker push "$FULL_IMAGE"; then
    log_err "镜像推送失败。常见原因："
    echo "  1. Docker Hub 中不存在仓库 ${IMAGE}，需先在网页端创建（建议设为 Public）"
    echo "  2. 登录账号无权写入该仓库（用户名拼写错误）"
    echo "  3. 网络受限，可配置镜像加速或调整代理"
    exit 1
fi

log_ok "推送完成：${FULL_IMAGE}"
echo ""
echo "在任意机器上拉取运行："
echo "  docker run -d --name floattrip -p 8765:8765 --env-file .env ${FULL_IMAGE}"
echo ""
echo "Docker Hub 页面：https://hub.docker.com/r/${IMAGE}"
