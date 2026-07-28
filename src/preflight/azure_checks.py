"""前置檢查 1–4：登入的訂閱、資源區域、provider 註冊、角色指派。

全部以 Azure CLI 的開發者身分執行，不使用 API key。
"""

from dataclasses import dataclass
from urllib.parse import urlparse

from preflight import (
    ALLOWED_REGIONS,
    RECOMMENDED_REGIONS,
    REQUIRED_PROVIDERS,
    ROLE_BOT_SERVICE_CONTRIBUTOR,
    ROLE_FOUNDRY_PROJECT_MANAGER,
    AzCliError,
    CheckResult,
    run_az_json,
)


@dataclass(frozen=True)
class ProjectLocation:
    """由 `FOUNDRY_PROJECT_ENDPOINT` 解析並向 Azure 查得的專案位置資訊。"""

    account_name: str
    project_name: str
    resource_group: str
    location: str
    account_id: str

    @property
    def project_scope(self) -> str:
        """專案範圍的 scope 字串。"""
        return f"{self.account_id}/projects/{self.project_name}"

    @property
    def resource_group_scope(self) -> str:
        """資源群組範圍的 scope 字串。"""
        subscription_id = self.account_id.split("/")[2]
        return f"/subscriptions/{subscription_id}/resourceGroups/{self.resource_group}"


def parse_endpoint(endpoint: str) -> tuple[str, str] | None:
    """從專案端點解析出 Foundry 帳戶名稱與專案名稱。

    端點格式為 `https://<account>.services.ai.azure.com/api/projects/<project>`。
    解析失敗時回傳 `None`，由呼叫端輸出「無法自動判定」而非假通過。
    """
    parsed = urlparse(endpoint)
    if not parsed.hostname:
        return None

    account_name = parsed.hostname.split(".")[0]
    segments = [segment for segment in parsed.path.split("/") if segment]
    if "projects" not in segments:
        return None

    project_index = segments.index("projects") + 1
    if project_index >= len(segments):
        return None

    return account_name, segments[project_index]


def check_subscription() -> CheckResult:
    """檢查 1：目前登入的訂閱。"""
    try:
        account = run_az_json(["account", "show"])
    except AzCliError as error:
        return CheckResult(
            index=1,
            title="登入的訂閱",
            passed=False,
            detail=f"無法取得目前訂閱：{error}",
            impact="沒有有效登入就無法連線 Foundry 專案，後續全部檢查都會失敗。",
            remediation=[
                "az login --use-device-code",
                "az account set --subscription <你的訂閱 ID>",
            ],
        )

    assert isinstance(account, dict)
    name = account.get("name", "（未知）")
    subscription_id = account.get("id", "（未知）")
    return CheckResult(
        index=1,
        title="登入的訂閱",
        passed=True,
        detail=f"{name}（{subscription_id}）— 請確認這是你預期使用的訂閱",
    )


def resolve_project_location(endpoint: str) -> ProjectLocation | AzCliError:
    """依專案端點查出 Foundry 帳戶所在的資源群組與區域。"""
    parsed = parse_endpoint(endpoint)
    if parsed is None:
        return AzCliError(
            "無法從 FOUNDRY_PROJECT_ENDPOINT 解析出帳戶與專案名稱。"
            "預期格式為 https://<account>.services.ai.azure.com/api/projects/<project>"
        )

    account_name, project_name = parsed
    try:
        accounts = run_az_json(
            [
                "cognitiveservices",
                "account",
                "list",
                "--query",
                f"[?name=='{account_name}'].{{location:location,rg:resourceGroup,id:id}}",
            ]
        )
    except AzCliError as error:
        return error

    if not accounts:
        return AzCliError(
            f"目前訂閱中找不到名為 {account_name} 的 Foundry 資源。"
            "請確認 .env 的專案端點與 az account show 顯示的訂閱一致。"
        )

    assert isinstance(accounts, list)
    account = accounts[0]
    return ProjectLocation(
        account_name=account_name,
        project_name=project_name,
        resource_group=account["rg"],
        location=account["location"],
        account_id=account["id"],
    )


def check_region(location: ProjectLocation | AzCliError) -> CheckResult:
    """檢查 2：資源區域。

    判準是 Foundry hosted agents 的**官方支援區域清單**（`ALLOWED_REGIONS`，查證日 2026-07-27）。
    區域錯誤是**現場無法修復**的問題：換區域等同重建整個 Foundry 專案與模型部署。
    台灣場次最常踩到的是 **East Asia 不在官方清單內**。
    """
    recommended = "、".join(ALLOWED_REGIONS[slug] for slug in RECOMMENDED_REGIONS)

    if isinstance(location, AzCliError):
        return CheckResult(
            index=2,
            title="資源區域",
            passed=False,
            detail=f"無法自動判定區域：{location}",
            impact="若專案不在 hosted agents 的官方支援區域，Lab 3 的部署會失敗。",
            remediation=[
                "手動確認：Foundry portal → 你的專案 → 概觀 → 檢視「位置」欄位。",
                "區域必須在 hosted agents 的官方支援清單內（East Asia 不在清單內）：",
                "    https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#region-availability",
            ],
            onsite_fixable=False,
        )

    display = ALLOWED_REGIONS.get(location.location)
    if display:
        note = "" if location.location in RECOMMENDED_REGIONS else f"；建議區域為 {recommended}（延遲較低）"
        return CheckResult(
            index=2,
            title="資源區域",
            passed=True,
            detail=f"{display}（資源群組 {location.resource_group}）{note}",
        )

    return CheckResult(
        index=2,
        title="資源區域",
        passed=False,
        detail=f"專案位於 {location.location}，不在 hosted agents 的官方支援清單內",
        impact="hosted agents 不支援此區域，Lab 3 與 Lab 4 無法完成。",
        remediation=[
            f"需要在支援的區域重新建立 Foundry 專案與模型部署，台灣場次建議 {recommended}。",
            "完整支援清單（East Asia 不在其中）：",
            "    https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#region-availability",
            "此步驟耗時且需重新部署模型，現場無法完成——請改走觀摩路徑（docs/06-fallback.md）。",
        ],
        onsite_fixable=False,
    )


def check_providers() -> CheckResult:
    """檢查 3：資源提供者註冊狀態。"""
    unregistered: list[str] = []
    errors: list[str] = []

    for namespace in REQUIRED_PROVIDERS:
        try:
            state = run_az_json(
                ["provider", "show", "--namespace", namespace, "--query", "registrationState"]
            )
        except AzCliError as error:
            errors.append(f"{namespace}：{error}")
            continue
        if state != "Registered":
            unregistered.append(namespace)

    if errors:
        return CheckResult(
            index=3,
            title="provider 註冊",
            passed=False,
            detail="；".join(errors),
            impact="無法確認 provider 狀態，Lab 4 發布到 Teams 可能失敗。",
            remediation=[f"az provider show --namespace {ns}" for ns in REQUIRED_PROVIDERS],
        )

    if not unregistered:
        return CheckResult(
            index=3,
            title="provider 註冊",
            passed=True,
            detail="、".join(REQUIRED_PROVIDERS),
        )

    return CheckResult(
        index=3,
        title="provider 註冊",
        passed=False,
        detail=f"{'、'.join(unregistered)} 未註冊",
        impact="Microsoft.BotService 未註冊時，Lab 4 發布到 Teams 會失敗。",
        remediation=[
            *[f"az provider register --namespace {ns}" for ns in unregistered],
            "註冊需要數分鐘。可先進行 Lab 1，Lab 3 前務必完成。",
        ],
    )


def check_role_assignments(location: ProjectLocation | AzCliError) -> CheckResult:
    """檢查 4：角色指派。

    查詢方式依 2026-07-27 的官方來源查證結果：

    - 以**角色定義 ID（GUID）**而非顯示名稱比對，因為 Foundry 的 RBAC 角色近期更名，
      更名推行期間顯示名稱可能不一致，但 ID 不變。
    - 必須帶 `--include-inherited` 與 `--include-groups`：學員常見的情況是在訂閱層取得
      `Owner`，或透過安全性群組取得權限；缺少這兩個旗標會把已具權限的學員誤判為失敗。
    """
    missing: list[str] = []

    if isinstance(location, AzCliError):
        return CheckResult(
            index=4,
            title="角色指派",
            passed=False,
            detail=f"無法判定查詢範圍：{location}",
            impact="無法確認權限。權限不足時 Lab 2 建立 agent 或 Lab 4 發布會失敗。",
            remediation=[
                "先修復檢查 2 的區域判定問題，本項才能自動查詢。",
                "手動確認：Azure portal → 你的 Foundry 專案 → 存取控制 (IAM) → 檢查角色指派。",
            ],
            onsite_fixable=False,
        )

    principal_id = _current_principal_id()
    if principal_id is None:
        return CheckResult(
            index=4,
            title="角色指派",
            passed=False,
            detail="無法取得目前登入者的 objectId",
            impact="無法確認權限。",
            remediation=["az ad signed-in-user show --query id --output tsv"],
        )

    checks = (
        (ROLE_FOUNDRY_PROJECT_MANAGER, location.project_scope, "專案範圍"),
        (ROLE_BOT_SERVICE_CONTRIBUTOR, location.resource_group_scope, "資源群組範圍"),
    )

    for (role_name, role_id), scope, scope_label in checks:
        try:
            assignments = run_az_json(
                [
                    "role",
                    "assignment",
                    "list",
                    "--scope",
                    scope,
                    "--role",
                    role_id,
                    "--assignee",
                    principal_id,
                    "--include-inherited",
                    "--include-groups",
                ]
            )
        except AzCliError as error:
            missing.append(f"{role_name}（{scope_label}）查詢失敗：{error}")
            continue
        if not assignments:
            missing.append(f"{role_name}（{scope_label}）")

    if not missing:
        return CheckResult(
            index=4,
            title="角色指派",
            passed=True,
            detail=f"{ROLE_FOUNDRY_PROJECT_MANAGER[0]}、{ROLE_BOT_SERVICE_CONTRIBUTOR[0]}",
        )

    return CheckResult(
        index=4,
        title="角色指派",
        passed=False,
        detail=f"缺少：{'；'.join(missing)}",
        impact="權限不足會導致 Lab 2 無法建立 agent，或 Lab 4 出現 403 AuthorizationFailed。",
        remediation=[
            "若你是訂閱擁有者，可自行指派（把 <objectId> 換成你的 objectId）：",
            f'az role assignment create --role "{ROLE_FOUNDRY_PROJECT_MANAGER[1]}" '
            f'--assignee-object-id "{principal_id}" --assignee-principal-type User '
            f'--scope "{location.project_scope}"',
            f'az role assignment create --role "{ROLE_BOT_SERVICE_CONTRIBUTOR[1]}" '
            f'--assignee-object-id "{principal_id}" --assignee-principal-type User '
            f'--scope "{location.resource_group_scope}"',
            "若你不是訂閱擁有者，需請訂閱管理員指派——這通常無法在現場完成。",
        ],
        onsite_fixable=False,
    )


def _current_principal_id() -> str | None:
    """取得目前登入者的 objectId。"""
    try:
        result = run_az_json(["ad", "signed-in-user", "show", "--query", "id"])
    except AzCliError:
        return None
    return result if isinstance(result, str) else None
