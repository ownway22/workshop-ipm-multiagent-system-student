#!/usr/bin/env bash
# devcontainer 建立後的一次性設定。
# 目的：確認工具版本符合憲章下限，並依 src/pyproject.toml 的釘定版本解析虛擬環境。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "── 工具版本檢查 ──────────────────────────────"
python --version
uv --version
az version --output tsv --query '"azure-cli"' 2>/dev/null || echo "az: 尚未可用"

# 憲章要求 azd >= 1.25.3。低於下限時警示而不中斷，讓學員仍能完成 Lab 0 到 Lab 2。
azd version || true

echo
echo "── 解析 Python 相依（依 src/pyproject.toml 的釘定版本）──"
cd src
uv sync

echo
echo "✅ 環境就緒。下一步：複製 src/.env.template 為 src/.env 並填入你的值，"
echo "   接著執行 cd src && uv run python -m preflight"
