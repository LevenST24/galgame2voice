#!/usr/bin/env bash
# ==============================================================================
# Galgame2Voice - Linux & macOS 启动脚本
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================================"
echo "         Galgame2Voice - 伴侣服务启动中 (Unix)        "
echo "======================================================"

# 1. 查找 Python 3.10+
PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi

if [ -z "$PYTHON_BIN" ]; then
    echo "[错误] 未找到 Python 环境，请先安装 Python 3.10 或更高版本。"
    exit 1
fi

# 检查 Python 版本
$PYTHON_BIN -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null || {
    echo "[错误] 当前 Python 版本过低，需要 Python 3.10+"
    exit 1
}

# 2. 虚拟环境与依赖检测
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ -f "$VENV_PYTHON" ]; then
    PYTHON_EXE="$VENV_PYTHON"
    if ! "$PYTHON_EXE" -c "import fastapi, uvicorn, aiosqlite" >/dev/null 2>&1; then
        echo "[提示] 正在安装或补全缺失的运行依赖..."
        "$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
    fi
else
    echo "[首次部署] 正在初始化虚拟环境 .venv ..."
    if command -v uv >/dev/null 2>&1; then
        uv venv "$SCRIPT_DIR/.venv"
        uv pip install -r "$SCRIPT_DIR/requirements.txt" --python "$SCRIPT_DIR/.venv/bin/python"
        PYTHON_EXE="$SCRIPT_DIR/.venv/bin/python"
    else
        $PYTHON_BIN -m venv "$SCRIPT_DIR/.venv"
        "$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip
        "$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
        PYTHON_EXE="$SCRIPT_DIR/.venv/bin/python"
    fi
fi

echo "[OK] Python 解释器: $PYTHON_EXE"

# 3. 准备必要运行目录
mkdir -p logs data audio

# 4. 启动服务
exec "$PYTHON_EXE" scripts/run_server.py "$@"
