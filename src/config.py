"""環境設定的載入與驗證。

本模組是 `main.py` 與 `preflight` **共用**的驗證邏輯（不是各自複製一份），
確保不同入口、不同時點的兩次執行判準完全一致
（contracts/preflight-contract.md 第 5 節）。

設計原則（FR-004、SC-016）：缺漏的必要環境變數 MUST 在**啟動階段一次全部列出**後終止，
MUST NOT 逐一失敗，也 MUST NOT 延後到呼叫模型時才以模糊錯誤失敗。
"""

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

#: 必要環境變數：名稱 → 取得方式（失敗訊息會直接印出，MUST 可照著操作）。
REQUIRED_ENV_VARS: dict[str, str] = {
    "FOUNDRY_PROJECT_ENDPOINT": "Foundry portal → 你的專案 → 首頁 → 複製「專案端點」",
    "MODEL_DEPLOYMENT_NAME": (
        "Foundry portal → 你的專案 → Deployments → 複製「部署名稱」（不是模型型號）"
    ),
}

#: `.env.template` 的佔位符形式，例如 `<TODO: 貼上你的專案端點>`。
#: 學員常見的錯誤是複製了範本卻忘了填值；佔位符是非空字串，若不特別排除就會通過
#: 「缺漏」檢查，而在後面的端點連線階段以難懂的錯誤失敗。這裡把它當成缺漏處理（FR-004）。
_PLACEHOLDER_PATTERN = re.compile(r"^<.*>$")

#: 環境變數缺漏時的結束碼（contracts/preflight-contract.md 第 4.3 節）。
EXIT_CODE_MISSING_ENV = 2

#: DevUI 的預設連接埠。
#: 交付包 MUST 明確傳入 port，MUST NOT 依賴 `agent_framework_devui.serve()` 的套件預設值
#: （research.md 決策 R09：實測預設為 8080，而實作指南舊稿誤寫為 8090）。
#: 2026-07-27 spike S6 確認 8080 可用。
DEFAULT_DEVUI_PORT = 8080

#: DevUI 的預設繫結位址。
#: MUST 是 loopback：`agent_framework_devui` 只允許在 loopback 位址上關閉驗證，
#: 繫結 `0.0.0.0` 且 `auth_enabled=False` 會直接拋 `ValueError`（spike S6 實測）。
#: 代價是服務會啟用 `Host` 標頭允許清單，非 loopback 的 Host 一律回 400；
#: Codespaces 轉發若因此失敗，備援路徑是繫結 `0.0.0.0` + `DEVUI_AUTH_TOKEN`。
DEFAULT_DEVUI_HOST = "127.0.0.1"

#: `.env` 的位置：與本模組同層（即 `src/.env`）。
_ENV_FILE = Path(__file__).resolve().parent / ".env"


class MissingEnvironmentError(RuntimeError):
    """必要環境變數缺漏。訊息中已含完整清單與取得方式。"""


@dataclass(frozen=True)
class EnvironmentSettings:
    """本交付包執行期所需的全部設定值。

    切換講師環境與學員環境時 MUST 只改 `.env` 的值，MUST NOT 改動程式碼或講義（FR-003）。
    """

    foundry_project_endpoint: str
    model_deployment_name: str
    devui_port: int


def load_env_file() -> None:
    """載入 `src/.env`。

    Microsoft Agent Framework **不會**自動載入 `.env`，程式進入點必須自行呼叫
    `load_dotenv()`，否則 `FOUNDRY_PROJECT_ENDPOINT` 之類的值不會出現在 `os.environ`。
    這是本交付包最常見的第一個踩點，因此集中在此處理。

    來源 M01（Get started with Agent Framework）：
    https://learn.microsoft.com/en-us/agent-framework/get-started/
    —— 官方入門範例即明示需自行載入環境設定，框架本身不代勞。
    """
    # 明確指定路徑，不依賴 find_dotenv() 由目前工作目錄往上尋找的行為。
    load_dotenv(dotenv_path=_ENV_FILE)


def find_missing_env_vars() -> list[str]:
    """回傳缺漏的必要環境變數名稱。

    下列三種情況都算缺漏：未設定、僅含空白、值仍是 `.env.template` 的佔位符。

    這是純函式，可直接被單元測試覆蓋，不需要真實的 Foundry 專案。
    """
    return [name for name in REQUIRED_ENV_VARS if not _is_filled(os.getenv(name, ""))]


def _is_filled(raw: str) -> bool:
    """判斷一個環境變數的值是否已被真正填寫。"""
    value = raw.strip()
    return bool(value) and not _PLACEHOLDER_PATTERN.match(value)


def format_missing_env_message(missing: list[str]) -> str:
    """把缺漏清單組成一次可讀完的錯誤訊息。"""
    lines = [
        "❌ 必要環境變數缺漏或尚未填寫，無法繼續。",
        "",
        f"共 {len(missing)} 項：",
    ]
    for name in missing:
        lines.append(f"  - {name}")
        lines.append(f"      取得方式：{REQUIRED_ENV_VARS[name]}")
    lines.extend(
        [
            "",
            "修復（可直接複製執行）：",
            "    cd src",
            "    cp .env.template .env",
            "接著編輯 `src/.env`，把上列變數的 `<TODO: ...>` 佔位符換成你自己的值，再重新執行：",
            "    uv run python -m preflight",
        ]
    )
    return "\n".join(lines)


def load_settings() -> EnvironmentSettings:
    """載入並驗證全部設定，缺漏時一次列出完整清單後拋出例外。

    Raises:
        MissingEnvironmentError: 有必要環境變數缺漏；訊息已含完整清單與修復步驟。
    """
    load_env_file()

    missing = find_missing_env_vars()
    if missing:
        raise MissingEnvironmentError(format_missing_env_message(missing))

    return EnvironmentSettings(
        foundry_project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"].strip(),
        model_deployment_name=os.environ[
            "MODEL_DEPLOYMENT_NAME"
        ].strip(),
        devui_port=_read_devui_port(),
    )


def load_settings_or_exit() -> EnvironmentSettings:
    """供程式進入點使用：驗證失敗時印出完整清單並以結束碼 2 終止。"""
    try:
        return load_settings()
    except MissingEnvironmentError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(EXIT_CODE_MISSING_ENV) from error


def _read_devui_port() -> int:
    """讀取 `DEVUI_PORT`，未設定或無法解析時退回交付包指定的預設值。"""
    raw = os.getenv("DEVUI_PORT", "").strip()
    if not raw:
        return DEFAULT_DEVUI_PORT
    try:
        return int(raw)
    except ValueError:
        print(
            f"⚠️  DEVUI_PORT 的值「{raw}」不是整數，改用預設值 {DEFAULT_DEVUI_PORT}。",
            file=sys.stderr,
        )
        return DEFAULT_DEVUI_PORT
