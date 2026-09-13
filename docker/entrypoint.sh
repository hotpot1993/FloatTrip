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

echo "[entrypoint] 数据目录就绪：$DATA_DIR"
echo "[entrypoint] 启动命令：$*"

# 执行 CMD 传入的命令（默认：python run.py，监听 0.0.0.0:8765）
exec "$@"
