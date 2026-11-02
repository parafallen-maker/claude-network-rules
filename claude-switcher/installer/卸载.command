#!/bin/bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
echo "卸载工具与守护进程；保留账号数据。只恢复已记录且之后未被用户修改的系统/代理设置。"
exec python3 "$REPO/installer/install.py" uninstall --source "$REPO"
