"""環境前置檢查的共用型別與工具。

契約：[contracts/preflight-contract.md](../../specs/001-ipm-workshop-delivery/contracts/preflight-contract.md)

設計要點（FR-006、FR-008）：

- 每一項檢查都回傳獨立的 `CheckResult`，**不早退**——某項失敗時仍繼續檢查其餘項目，
  一次呈現完整狀況。
- 失敗項 MUST 附「可直接複製執行」的修復指令或編號步驟。
- 現場無法修復的項目（區域、租戶、權限）MUST 明確標示，讓學員盡早改走觀摩路徑。
"""

import json
import shutil
import subprocess
from dataclasses import dataclass, field

#: Foundry hosted agents 官方支援的區域（slug → 顯示名稱）。
#: 查證日期 **2026-07-27**，來源：
#: https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#region-availability
#: 官方註明「This list will be updated as additional regions become available.」，
#: 因此本清單 MUST 定期重查；**East Asia 不在清單內**，這是台灣場次最常見的踩點。
ALLOWED_REGIONS: dict[str, str] = {
    "australiaeast": "Australia East",
    "brazilsouth": "Brazil South",
    "canadacentral": "Canada Central",
    "canadaeast": "Canada East",
    "centralus": "Central US",
    "eastus": "East US",
    "eastus2": "East US 2",
    "francecentral": "France Central",
    "germanywestcentral": "Germany West Central",
    "italynorth": "Italy North",
    "japaneast": "Japan East",
    "japanwest": "Japan West",
    "koreacentral": "Korea Central",
    "northcentralus": "North Central US",
    "norwayeast": "Norway East",
    "polandcentral": "Poland Central",
    "southafricanorth": "South Africa North",
    "southcentralus": "South Central US",
    "southeastasia": "Southeast Asia",
    "southindia": "South India",
    "spaincentral": "Spain Central",
    "swedencentral": "Sweden Central",
    "switzerlandnorth": "Switzerland North",
    "switzerlandwest": "Switzerland West",
    "uaenorth": "UAE North",
    "uksouth": "UK South",
    "ukwest": "UK West",
    "westcentralus": "West Central US",
    "westeurope": "West Europe",
    "westus": "West US",
    "westus3": "West US 3",
}

#: 台灣場次的**建議**區域（延遲最低）。不在這兩個區域不算失敗，只是互動時延遲較高。
RECOMMENDED_REGIONS: tuple[str, ...] = ("southeastasia", "japaneast")

#: 需為 `Registered` 的資源提供者（憲章「平台硬性限制」）。
REQUIRED_PROVIDERS: tuple[str, ...] = (
    "Microsoft.CognitiveServices",
    "Microsoft.BotService",
)

#: 角色定義 ID。MUST 以 GUID 而非顯示名稱查詢：Foundry 的 RBAC 角色近期由
#: `Azure AI *` 更名為 `Foundry *`，更名推行期間兩種名稱可能同時出現，但角色 ID 不變。
#: 查證來源與完整說明見 contracts/preflight-contract.md 第 9 節（T016a，2026-07-27）。
ROLE_FOUNDRY_PROJECT_MANAGER = ("Foundry Project Manager", "eadc314b-1a2d-4efa-be10-5d325db5065e")
ROLE_BOT_SERVICE_CONTRIBUTOR = (
    "Azure Bot Service Contributor Role",
    "9fc6112f-f48e-4e27-8b09-72a5c94e4ae9",
)


@dataclass(frozen=True)
class CheckResult:
    """單一檢查項的結果。"""

    index: int
    title: str
    passed: bool
    detail: str
    impact: str = ""
    remediation: list[str] = field(default_factory=list)
    onsite_fixable: bool = True


class AzCliError(RuntimeError):
    """`az` 指令執行失敗或不可用。"""


def az_available() -> bool:
    """Azure CLI 是否可用。"""
    return shutil.which("az") is not None


def run_az_json(args: list[str], *, timeout: int = 60) -> object:
    """執行 `az ... --output json` 並回傳解析後的物件。

    Raises:
        AzCliError: `az` 不可用、逾時、非零結束碼，或輸出不是合法 JSON。
    """
    if not az_available():
        raise AzCliError("找不到 az 指令。請確認你在 devcontainer 內執行。")

    command = ["az", *args, "--output", "json"]
    try:
        completed = subprocess.run(  # noqa: S603 - 參數為程式內組出的固定清單，非使用者輸入
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise AzCliError(f"az 指令逾時（{timeout} 秒）：{' '.join(args)}") from error

    if completed.returncode != 0:
        raise AzCliError((completed.stderr or completed.stdout).strip())

    output = completed.stdout.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise AzCliError(f"az 輸出不是合法 JSON：{output[:200]}") from error


__all__ = [
    "ALLOWED_REGIONS",
    "RECOMMENDED_REGIONS",
    "REQUIRED_PROVIDERS",
    "ROLE_BOT_SERVICE_CONTRIBUTOR",
    "ROLE_FOUNDRY_PROJECT_MANAGER",
    "AzCliError",
    "CheckResult",
    "az_available",
    "run_az_json",
]
